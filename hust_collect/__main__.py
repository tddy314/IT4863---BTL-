"""CLI: python -m hust_collect --help."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import io
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from lxml import etree
import requests

from .processing import (digest, extract_document, extract_html, fold_text,
                         is_document, normalize_text, sitemap_links, tree_for)

LOG = logging.getLogger('hust_collect')
ROOT = 'hust.edu.vn'
USER_AGENT = 'HustCourseCrawler/1.0'
SKIP_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico', '.css', '.js',
            '.woff', '.woff2', '.ttf', '.zip', '.rar', '.7z', '.mp3', '.mp4', '.avi', '.exe'}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def in_scope(host: str) -> bool:
    return host == ROOT or host.endswith('.' + ROOT)


def canonical_url(value: str, base: str = '') -> str | None:
    try:
        parts = urlsplit(urljoin(base, value.strip()))
        host = (parts.hostname or '').lower().rstrip('.')
        if (parts.scheme.lower() not in ('http', 'https') or not in_scope(host)
                or parts.username is not None or parts.password is not None
                or '*' in host or not re.fullmatch(r'[a-z0-9.-]+', host)
                or any(not label or label.startswith('-') or label.endswith('-') for label in host.split('.'))):
            return None
        if parts.port not in (None, 80 if parts.scheme.lower() == 'http' else 443):
            return None
        path = parts.path or '/'
        if Path(path.lower()).suffix in SKIP_EXT:
            return None
        # Preserve meaningful query parameters; discard tracking only.
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
        if len(query) > 5 or len(value) > 2048:
            return None
        if re.search(r'/(?:login|logout|signin|search|calendar)(?:/|$)', path, re.I):
            return None
        return urlunsplit((parts.scheme.lower(), host, path, urlencode(sorted(query)), ''))
    except (ValueError, UnicodeError):
        return None


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / 'raw').mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'crawl.sqlite3', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS domains (
          host TEXT PRIMARY KEY, source TEXT NOT NULL, discovered_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS urls (
          url TEXT PRIMARY KEY, host TEXT NOT NULL, source_url TEXT, depth INTEGER NOT NULL,
          priority INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
          next_fetch REAL NOT NULL DEFAULT 0, checked_at TEXT, http_status INTEGER,
          etag TEXT, last_modified TEXT, raw_hash TEXT, raw_path TEXT, mime TEXT, encoding TEXT,
          final_url TEXT, failures INTEGER NOT NULL DEFAULT 0, missing_count INTEGER NOT NULL DEFAULT 0,
          error TEXT);
        CREATE INDEX IF NOT EXISTS due_urls ON urls(next_fetch, priority);
        CREATE INDEX IF NOT EXISTS host_due_urls ON urls(host, next_fetch, priority);
        CREATE TABLE IF NOT EXISTS documents (
          id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, content_hash TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, record TEXT NOT NULL);
        ''')

    def discover(self, host, source):
        if in_scope(host) and re.fullmatch(r'[a-z0-9.-]+', host) and '*' not in host:
            self.db.execute('INSERT OR IGNORE INTO domains VALUES (?,?,?)', (host, source, utc_now()))

    def enqueue(self, value, source=None, depth=0, priority=10, max_urls=50000, max_hosts=30):
        url = canonical_url(value)
        if not url:
            return False
        host = urlsplit(url).hostname
        self.discover(host, source or 'seed')
        if self.db.execute('SELECT 1 FROM urls WHERE url=?', (url,)).fetchone():
            return False
        if self.db.execute('SELECT COUNT(*) FROM urls').fetchone()[0] >= max_urls:
            return False
        known_host = self.db.execute('SELECT 1 FROM urls WHERE host=? LIMIT 1', (host,)).fetchone()
        if not known_host and self.db.execute('SELECT COUNT(DISTINCT host) FROM urls').fetchone()[0] >= max_hosts:
            return False
        self.db.execute('INSERT INTO urls(url,host,source_url,depth,priority) VALUES (?,?,?,?,?)',
                        (url, host, source, depth, priority))
        return True

    def update(self, url, **fields):
        self.db.execute('UPDATE urls SET ' + ','.join(k + '=?' for k in fields) + ' WHERE url=?',
                        (*fields.values(), url))

    def raw(self, body):
        value = digest(body)
        relative = Path('raw') / (value + '.bin')
        target = self.directory / relative
        if not target.exists():
            target.write_bytes(body)
        return value, relative.as_posix()

    def document(self, url, record):
        # Metadata changes (title/date) count as updates as well as body changes.
        content_hash = digest(json.dumps({k: record.get(k) for k in ('title', 'content', 'published_at')},
                                         ensure_ascii=False, sort_keys=True).encode('utf-8'))
        previous = self.db.execute('SELECT record,content_hash FROM documents WHERE url=?', (url,)).fetchone()
        now = utc_now()
        old = json.loads(previous['record']) if previous else {}
        record.update(id=digest(url.encode()), url=url, content_hash=content_hash,
                      first_seen_at=old.get('first_seen_at', now),
                      updated_at=old.get('updated_at', now) if previous and previous['content_hash'] == content_hash else now)
        self.db.execute('''INSERT INTO documents VALUES (?,?,?,1,?)
          ON CONFLICT(url) DO UPDATE SET content_hash=excluded.content_hash,
          active=1,record=excluded.record''',
                        (record['id'], url, content_hash, json.dumps(record, ensure_ascii=False)))
        return 'new' if not previous else ('unchanged' if previous['content_hash'] == content_hash else 'updated')


class FetchError(Exception):
    pass


class Crawler:
    def __init__(self, store, args, session=None):
        self.store, self.args = store, args
        self.session = session or requests.Session()
        self.session.headers.update({'User-Agent': args.user_agent})
        self.last_request = {}
        self.robots = {}
        self.robots_failures = {}
        self.visits = Counter()
        self.config = json.loads(Path(args.extractors).read_text(encoding='utf-8')) if args.extractors else {}
        self.segmenter = None
        if args.vncorenlp_dir:
            import py_vncorenlp
            self.segmenter = py_vncorenlp.VnCoreNLP(annotators=['wseg'], save_dir=str(Path(args.vncorenlp_dir).resolve()))

    def enqueue(self, url, source=None, depth=0, priority=10):
        return self.store.enqueue(url, source, depth, priority, self.args.max_urls, self.args.max_domains)

    def wait(self, url, delay):
        host = urlsplit(url).hostname
        remaining = self.last_request.get(host, 0) + delay - time.monotonic()
        while remaining > 0:
            time.sleep(min(remaining, 1))
            remaining = self.last_request.get(host, 0) + delay - time.monotonic()
        self.last_request[host] = time.monotonic()

    def request(self, url, headers=None, for_robots=False):
        original_host = urlsplit(url).hostname
        for _ in range(6):
            safe = canonical_url(url)
            if not safe:
                raise FetchError('Out-of-scope or unsupported redirect: ' + url)
            delay = self.args.delay
            if not for_robots:
                rules = self.rules(safe)
                if not rules.can_fetch(self.args.user_agent, safe):
                    raise FetchError('robots.txt disallows: ' + safe)
                delay = max(delay, rules.crawl_delay(self.args.user_agent) or rules.crawl_delay('*') or 0)
                rate = rules.request_rate(self.args.user_agent) or rules.request_rate('*')
                if rate and rate.requests > 0:
                    delay = max(delay, rate.seconds / rate.requests)
            self.wait(safe, delay)
            # Never send validators from one hostname to another on redirects.
            sent = headers if urlsplit(safe).hostname == original_host else None
            with self.session.get(safe, headers=sent, timeout=self.args.timeout,
                                  stream=True, allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location')
                    if not location:
                        raise FetchError('Redirect without Location')
                    url = urljoin(safe, location)
                    continue
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > self.args.max_bytes:
                        raise FetchError('Response exceeds --max-bytes')
                    chunks.append(chunk)
                return safe, response.status_code, dict(response.headers), b''.join(chunks)
        raise FetchError('Too many redirects')

    def rules(self, url):
        parts = urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
        if origin in self.robots:
            return self.robots[origin]
        failure = self.robots_failures.get(origin)
        if failure and time.monotonic() < failure[0]:
            raise FetchError(f'robots.txt retry deferred: {failure[1]}')
        robot_url = origin + '/robots.txt'
        try:
            _, status, _, body = self.request(robot_url, for_robots=True)
            if status == 429 or status >= 500:
                raise FetchError(f'robots.txt unavailable ({status}); retry later')
            if not 200 <= status < 500:
                raise FetchError(f'Unexpected robots.txt status: {status}')
        except (requests.RequestException, FetchError, OSError, ValueError) as exc:
            self.robots_failures[origin] = (time.monotonic() + 300, str(exc))
            raise
        self.robots_failures.pop(origin, None)
        parser = RobotFileParser(robot_url)
        if status in (401, 403):
            parser.parse(['User-agent: *', 'Disallow: /'])
        elif 200 <= status < 300:
            parser.parse(body.decode('utf-8-sig', errors='replace').splitlines())
        elif 400 <= status < 500:
            parser.parse([])
        else:
            raise FetchError(f'Unexpected robots.txt status: {status}')
        self.robots[origin] = parser
        for sitemap in parser.site_maps() or []:
            self.enqueue(canonical_url(sitemap, origin) or '', robot_url, 0, 1)
        self.enqueue(origin + '/sitemap.xml', robot_url, 0, 2)
        return parser

    def process(self, row, body, mime, final_url, encoding=None):
        url = row['url']
        looks_xml = body.lstrip().startswith((b'<?xml', b'<urlset', b'<sitemapindex', b'<rss', b'<feed'))
        if not is_document(final_url, mime) and (('xml' in mime and mime != 'application/xhtml+xml') or looks_xml or urlsplit(final_url).path.endswith('.xml.gz')):
            if body.startswith(b'\x1f\x8b'):
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                    body = stream.read(self.args.max_bytes + 1)
                if len(body) > self.args.max_bytes:
                    raise ValueError('Expanded sitemap exceeds --max-bytes')
            for link in sitemap_links(body):
                normalized = canonical_url(link, final_url)
                if normalized:
                    self.enqueue(normalized, url, 0, 3)
            return None
        if is_document(final_url, mime):
            record = extract_document(body, final_url, mime, self.args.tika_url, self.args.tika_timeout)
        elif mime in ('text/html', 'application/xhtml+xml') or (not mime and b'<html' in body[:1024].lower()):
            tree = tree_for(body, encoding)
            # Extract links before removing navigation: it is useful for discovery.
            links = tree.xpath('//a/@href | //link[@rel="alternate" and (@type="application/rss+xml" or @type="application/atom+xml")]/@href')
            bases = tree.xpath('//base/@href')
            base = urljoin(final_url, bases[0]) if bases else final_url
            for href in links:
                target = canonical_url(href, base)
                if not target:
                    continue
                host = urlsplit(target).hostname
                self.store.discover(host, url)
                if host != row['host']:
                    self.enqueue(f'https://{host}/', url, 0, 0)
                if row['depth'] < self.args.max_depth:
                    priority = 4 if is_document(target) else 8
                    self.enqueue(target, url, row['depth'] + 1, priority)
            config = self.config.get(urlsplit(final_url).hostname, {})
            record = extract_html(body, final_url, config, self.args.min_chars, encoding)
        else:
            return None
        if record is None:
            return None
        # URL normalization does not alter the original Vietnamese text.
        record['normalized_content'] = normalize_text(record['content']).lower()
        record['folded_content'] = fold_text(record['normalized_content'])
        record['duplicate_group'] = digest(record['normalized_content'].encode('utf-8'))
        record['segmented_content'] = ('\n'.join(self.segmenter.word_segment(record['content']))
                                        if self.segmenter else None)
        record['segmentation'] = 'vncorenlp' if self.segmenter else 'not_applied'
        record.update(domain=urlsplit(final_url).hostname, source_url=row['source_url'],
                      final_url=final_url, fetched_at=utc_now())
        return self.store.document(url, record)

    def visit(self, row):
        url = row['url']
        validators = {}
        if row['etag']:
            validators['If-None-Match'] = row['etag']
        if row['last_modified']:
            validators['If-Modified-Since'] = row['last_modified']
        try:
            final_url, status, headers, body = self.request(url, validators)
            self.store.discover(urlsplit(final_url).hostname, url)
            headers = {k.lower(): v for k, v in headers.items()}
            now = time.time()
            common = dict(checked_at=utc_now(), http_status=status, final_url=final_url,
                          next_fetch=now + self.args.revisit_hours * 3600)
            if status == 304:
                # Reuse saved bytes for link discovery and retry failed extraction.
                if not row['raw_path']:
                    raise FetchError('304 without a saved body')
                try:
                    self.process(row, (self.store.directory / row['raw_path']).read_bytes(),
                                 row['mime'] or '', final_url, row['encoding'])
                except Exception as exc:
                    self.store.update(url, **common, state='parse_error', error=str(exc)[:1000])
                    return 'parse_error'
                self.store.update(url, **common, state='fetched', failures=0, missing_count=0, error=None)
                return 'not_modified'
            if status in (404, 410):
                missing = row['missing_count'] + 1
                if status == 410 or missing >= 3:
                    self.store.db.execute('UPDATE documents SET active=0 WHERE url=?', (url,))
                self.store.update(url, **common, state='missing', missing_count=missing, error=f'HTTP {status}')
                return 'missing'
            if status != 200:
                raise FetchError(f'HTTP {status}')
            mime = headers.get('content-type', '').split(';')[0].strip().lower()
            charset = re.search(r'charset\s*=\s*["\']?([^;\s"\']+)', headers.get('content-type', ''), re.I)
            encoding = charset.group(1) if charset else None
            raw_hash, raw_path = self.store.raw(body)
            self.store.update(url, **common, state='parse_error', raw_hash=raw_hash, raw_path=raw_path,
                              mime=mime, encoding=encoding, etag=headers.get('etag'),
                              last_modified=headers.get('last-modified'), failures=0, missing_count=0,
                              error='Extraction interrupted; run reprocess')
            self.store.db.commit()  # Preserve download even when parsing/Tika fails.
            try:
                outcome = self.process(row, body, mime, final_url, encoding)
            except Exception as exc:
                self.store.update(url, state='parse_error', error=str(exc)[:1000])
                LOG.warning('Extraction failed %s: %s', url, exc)
                return 'parse_error'
            self.store.update(url, state='fetched', error=None)
            return outcome or 'navigation_or_unsupported'
        except (requests.RequestException, FetchError, OSError, ValueError) as exc:
            failures = row['failures'] + 1
            retry = min(24 * 3600, 300 * 2 ** min(failures - 1, 8))
            self.store.update(url, state='retry', failures=failures, checked_at=utc_now(),
                              next_fetch=time.time() + retry, error=str(exc)[:1000])
            LOG.warning('Fetch failed %s: %s', url, exc)
            return 'retry'

    def run(self):
        counts = Counter()
        attempted = set()
        state_filter = " AND state='pending'" if self.args.new_only else ''
        while len(attempted) < self.args.max_pages:
            # Round-robin hosts: bounded query per hostname, no scan of the entire frontier.
            candidate = None
            hosts = self.store.db.execute('SELECT DISTINCT host FROM urls WHERE next_fetch<=?' + state_filter, (time.time(),)).fetchall()
            for host_row in sorted(hosts, key=lambda r: (self.visits[r['host']], r['host'])):
                host = host_row['host']
                if self.visits[host] >= self.args.per_domain:
                    continue
                rows = self.store.db.execute('SELECT * FROM urls WHERE host=? AND next_fetch<=?' + state_filter + '''
                    ORDER BY priority, next_fetch, url LIMIT ?''',
                    (host, time.time(), self.args.per_domain + 1)).fetchall()
                candidate = next((r for r in rows if r['url'] not in attempted), None)
                if candidate:
                    break
            if candidate is None:
                break
            attempted.add(candidate['url'])
            self.visits[candidate['host']] += 1
            result = self.visit(candidate)
            self.store.db.commit()
            counts[result] += 1
            LOG.info('[%d/%d] %s %s', len(attempted), self.args.max_pages, result, candidate['url'])
        return dict(counts)


def load_lines(path):
    return [line.strip() for line in Path(path).read_text(encoding='utf-8-sig').splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def seed_reference(crawler, path):
    """Read discovery evidence, never import old extracted content as a new crawl."""
    rows = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(rows, list):
        raise ValueError('Reference JSON must contain a list of records')
    seeded = 0
    linked_hosts = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = canonical_url(str(row.get('url') or ''))
        if not url:
            continue
        seeded += bool(crawler.enqueue(url, 'reference:' + str(path), 0, 0))
        for link in row.get('olinks') or []:
            if not isinstance(link, dict):
                continue
            target = canonical_url(str(link.get('dest') or ''), url)
            if target:
                host = urlsplit(target).hostname
                if host not in linked_hosts:
                    crawler.store.discover(host, 'reference-link:' + url)
                    linked_hosts.add(host)
    for host in sorted(linked_hosts):
        crawler.enqueue('https://' + host + '/', 'reference-links', 0, 0)
    LOG.info('Reference: %d article/document URLs seeded, %d linked HUST hosts found; old content not imported',
             seeded, len(linked_hosts))


def discover(args, store):
    hosts = set()
    if args.input:
        for line in load_lines(args.input):
            url = canonical_url(line if '://' in line else 'https://' + line)
            if url:
                host = urlsplit(url).hostname
                hosts.add(host)
                store.discover(host, 'file:' + args.input)
    if args.crtsh:
        try:
            response = requests.get('https://crt.sh/', params={'q': '%.hust.edu.vn', 'output': 'json'},
                                    headers={'User-Agent': USER_AGENT}, timeout=60)
            response.raise_for_status()
            for entry in response.json():
                for name in str(entry.get('name_value', '')).splitlines():
                    # A wildcard certificate is not evidence for any concrete child host.
                    url = canonical_url('https://' + name)
                    if url:
                        host = urlsplit(url).hostname
                        hosts.add(host)
                        store.discover(host, 'crt.sh')
        except (requests.RequestException, ValueError) as exc:
            LOG.error('crt.sh unavailable; imported hosts remain available: %s', exc)
            if not hosts:
                raise SystemExit(1)
    store.db.commit()
    path = store.directory / 'subdomains.txt'
    path.write_text('\n'.join(r['host'] for r in store.db.execute('SELECT host FROM domains ORDER BY host')) + '\n', encoding='utf-8')
    print(json.dumps({'imported_hosts': len(hosts), 'list': str(path)}, ensure_ascii=False))


def export_data(store, output=None):
    target = Path(output) if output else store.directory / 'documents.jsonl'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.tmp')
    count = 0
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        for row in store.db.execute('SELECT record FROM documents WHERE active=1 ORDER BY url'):
            stream.write(row['record'] + '\n')
            count += 1
    temporary.replace(target)
    # Keep the enriched JSONL for Lucene and export exactly the supplied sample schema.
    sample_target = store.directory / 'all_extracted_results.json'
    sample_temporary = sample_target.with_name(sample_target.name + '.tmp')
    seen_destinations = set()
    with sample_temporary.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write('[\n')
        for row in store.db.execute('SELECT record FROM documents WHERE active=1 ORDER BY url'):
            record = json.loads(row['record'])
            destination = record.get('final_url') or record['url']
            if destination in seen_destinations:
                continue
            compatible = {'url': destination, 'title': record['title'],
                          'content': record['content'], 'olinks': record.get('olinks') or []}
            if seen_destinations:
                stream.write(',\n')
            stream.write(json.dumps(compatible, ensure_ascii=False, indent=2))
            seen_destinations.add(destination)
        stream.write('\n]\n')
    sample_temporary.replace(sample_target)
    return {'documents': count, 'output': str(target), 'sample_format_documents': len(seen_destinations),
            'sample_format_output': str(sample_target)}


def parser():
    p = argparse.ArgumentParser(description='Discover HUST hosts, crawl incrementally, extract and export JSONL.')
    p.add_argument('--data-dir', default='data', help='SQLite, raw downloads and output directory')
    subs = p.add_subparsers(dest='command', required=True)
    d = subs.add_parser('discover', help='Import Subfinder hosts and/or query certificate transparency')
    d.add_argument('--input', help='Hostname/URL file (one per line)')
    d.add_argument('--crtsh', action='store_true')
    c = subs.add_parser('crawl', help='Discover links and process due URLs; rerun for updates')
    c.add_argument('--seeds', default='seeds.txt')
    c.add_argument('--include-discovered', action='store_true', help='Seed roots of hosts stored by discover')
    c.add_argument('--reference-json', help='Seed URLs and discover linked hosts from a sample JSON array; does not import its content')
    c.add_argument('--max-pages', type=int, default=100)
    c.add_argument('--new-only', action='store_true', help='Only process pending URLs; skip revisits and retries for this run')
    c.add_argument('--per-domain', type=int, default=30)
    c.add_argument('--max-domains', type=int, default=30)
    c.add_argument('--max-urls', type=int, default=50000)
    c.add_argument('--max-depth', type=int, default=6)
    c.add_argument('--delay', type=float, default=2)
    c.add_argument('--timeout', type=float, default=20)
    c.add_argument('--max-bytes', type=int, default=20 * 1024 * 1024)
    c.add_argument('--revisit-hours', type=float, default=24)
    c.add_argument('--user-agent', default=USER_AGENT)
    r = subs.add_parser('reprocess', help='Re-extract saved downloads without recrawling')
    r.set_defaults(max_bytes=20 * 1024 * 1024, max_urls=50000, max_domains=30, max_depth=6,
                   user_agent=USER_AGENT)
    for sub in (c, r):
        sub.add_argument('--extractors', default='extractors.json')
        sub.add_argument('--tika-url', help='Tika server URL, e.g. http://127.0.0.1:9998')
        sub.add_argument('--tika-timeout', type=float, default=120)
        sub.add_argument('--min-chars', type=int, default=200)
        sub.add_argument('--vncorenlp-dir', help='Optional existing VnCoreNLP model directory (requires py_vncorenlp and Java)')
    e = subs.add_parser('export', help='Export current active documents for Lucene ingestion')
    e.add_argument('--output')
    subs.add_parser('stats', help='Show discovered hosts, frontier state and extracted document counts')
    return p


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    p = parser()
    args = p.parse_args()
    if args.command == 'discover' and not (args.input or args.crtsh):
        p.error('discover requires --input and/or --crtsh')
    for name in ('max_pages', 'per_domain', 'max_domains', 'max_urls', 'max_bytes', 'timeout', 'revisit_hours', 'tika_timeout', 'min_chars'):
        if hasattr(args, name) and getattr(args, name) <= 0:
            p.error(name + ' must be positive')
    for name in ('delay', 'max_depth'):
        if hasattr(args, name) and getattr(args, name) < 0:
            p.error(name + ' must be nonnegative')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store = Store(args.data_dir)
    try:
        if args.command == 'discover':
            discover(args, store)
        elif args.command == 'crawl':
            crawler = Crawler(store, args)
            for line in load_lines(args.seeds):
                crawler.enqueue(line if '://' in line else 'https://' + line, 'seed', 0, 0)
            if args.reference_json:
                seed_reference(crawler, args.reference_json)
            if args.include_discovered:
                for row in store.db.execute('SELECT host FROM domains ORDER BY host').fetchall():
                    crawler.enqueue('https://' + row['host'] + '/', 'discovery', 0, 0)
            store.db.commit()
            try:
                print(json.dumps(crawler.run(), ensure_ascii=False))
            except KeyboardInterrupt:
                LOG.info('Interrupted; saved state will be reused on the next run')
            finally:
                store.db.commit()
                print(json.dumps(export_data(store), ensure_ascii=False))
        elif args.command == 'reprocess':
            crawler = Crawler(store, args)
            counts = Counter()
            for row in store.db.execute("SELECT * FROM urls WHERE raw_path IS NOT NULL AND state != 'missing'").fetchall():
                try:
                    result = crawler.process(row, (store.directory / row['raw_path']).read_bytes(),
                                             row['mime'] or '', row['final_url'] or row['url'], row['encoding'])
                    store.update(row['url'], state='fetched', error=None)
                    counts[result or 'navigation_or_unsupported'] += 1
                except Exception as exc:
                    store.update(row['url'], state='parse_error', error=str(exc)[:1000])
                    counts['parse_error'] += 1
                store.db.commit()
            print(json.dumps(dict(counts), ensure_ascii=False))
            print(json.dumps(export_data(store), ensure_ascii=False))
        elif args.command == 'export':
            print(json.dumps(export_data(store, args.output), ensure_ascii=False))
        elif args.command == 'stats':
            result = {
                'discovered_hosts': store.db.execute('SELECT COUNT(*) FROM domains').fetchone()[0],
                'scheduled_hosts': store.db.execute('SELECT COUNT(DISTINCT host) FROM urls').fetchone()[0],
                'url_states': dict(store.db.execute('SELECT state,COUNT(*) FROM urls GROUP BY state').fetchall()),
                'active_documents': store.db.execute('SELECT COUNT(*) FROM documents WHERE active=1').fetchone()[0],
                'documents_by_host': dict(store.db.execute('''SELECT u.host,COUNT(*) FROM documents d
                     JOIN urls u ON u.url=d.url WHERE d.active=1 GROUP BY u.host''').fetchall()),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == '__main__':
    main()

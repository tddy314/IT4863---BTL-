"""Offline extraction: preserve source text; normalize without pretending to segment words."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

DOCUMENT_EXTENSIONS = {'.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.odt', '.rtf', '.txt'}


def normalize_text(value: str) -> str:
    value = unicodedata.normalize('NFC', value).replace('\x00', '')
    return re.sub(r'\s+', ' ', value).strip()


def fold_text(value: str) -> str:
    value = unicodedata.normalize('NFD', value.lower().replace('đ', 'd').replace('Đ', 'd'))
    return ''.join(c for c in value if unicodedata.category(c) != 'Mn')


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def is_document(url: str, mime: str = '') -> bool:
    ext = PurePosixPath(urlsplit(url).path.lower()).suffix
    return (ext in DOCUMENT_EXTENSIONS or mime == 'application/pdf'
            or 'officedocument' in mime or 'msword' in mime or 'ms-excel' in mime
            or 'ms-powerpoint' in mime or 'opendocument' in mime or mime == 'application/rtf')


def tree_for(body: bytes, encoding: str | None = None):
    # Honor explicit HTTP encoding, otherwise lxml detects BOM / HTML meta charset.
    if not encoding and not re.search(br'charset\s*=', body[:4096], re.I):
        try:
            body.decode('utf-8')
            encoding = 'utf-8'
        except UnicodeDecodeError:
            pass
    if encoding:
        try:
            body = body.decode(encoding, errors='replace')
        except LookupError:
            pass
    return html.fromstring(body, parser=html.HTMLParser(no_network=True))


def clean_node(node) -> str:
    node = copy.deepcopy(node)
    for bad in node.xpath('.//script | .//style | .//nav | .//header | .//footer | .//aside | .//form | .//noscript'):
        bad.drop_tree()
    return normalize_text(' '.join(node.itertext()))


def first(tree, xpath: str) -> str:
    values = tree.xpath(xpath)
    if not isinstance(values, list):
        values = [values]
    if not values:
        return ''
    value = values[0]
    return normalize_text(' '.join(value.itertext()) if hasattr(value, 'itertext') else str(value))


def outgoing_links(tree, url: str) -> list[dict]:
    """Keep link evidence including external destinations; crawl scope is separate.

    atext: visible anchor text. etext: visible text of its immediate parent.
    Preserve order, repeated anchors and fragments for compatibility with the sample.
    """
    bases = tree.xpath('//base/@href')
    base = urljoin(url, bases[0]) if bases else url
    links = []
    for anchor in tree.xpath('//a[@href]'):
        href = anchor.get('href', '').strip()
        if not href:
            continue
        try:
            destination = urljoin(base, href)
            parts = urlsplit(destination)
            if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
                continue
            if parts.username is not None or parts.password is not None:
                continue
        except ValueError:
            continue
        parent = anchor.getparent()
        links.append({'atext': clean_node(anchor), 'dest': destination,
                      'etext': clean_node(parent) if parent is not None else clean_node(anchor)})
    return links


def article_metadata(tree) -> dict:
    result = {}
    for meta in tree.xpath('//meta[@content]'):
        key = (meta.get('property') or meta.get('name') or '').lower()
        result[key] = meta.get('content')
    for script in tree.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            nodes = json.loads(script)
        except (ValueError, TypeError):
            continue
        pending = nodes if isinstance(nodes, list) else [nodes]
        while pending:
            node = pending.pop()
            if not isinstance(node, dict):
                continue
            graph = node.get('@graph', [])
            pending.extend(graph if isinstance(graph, list) else [graph])
            kinds = node.get('@type', [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if isinstance(kinds, list) and any(k in ('Article', 'NewsArticle', 'BlogPosting') for k in kinds):
                result['is_article'] = True
                for key in ('headline', 'datePublished', 'author'):
                    if key in node:
                        result[key] = node[key]
    return result


def extract_html(body: bytes, url: str, config: dict, min_chars: int, encoding=None):
    tree = tree_for(body, encoding)
    meta = article_metadata(tree)
    title = first(tree, config.get('title_xpath', '//h1')) or str(meta.get('og:title', '')) or first(tree, '//title')
    published = first(tree, config.get('date_xpath', '//time/@datetime')) or meta.get('article:published_time') or meta.get('datePublished')
    custom = config.get('content_xpath')
    # Known body containers are stronger evidence than generic <main>/<article>.
    known = ("//*[@id='bodytext' or @id='news-bodyhtml' or @itemprop='articleBody' or @id='article-content']"
             " | //*[contains(concat(' ',normalize-space(@class),' '),' bodytext ')]"
             " | //*[contains(concat(' ',normalize-space(@class),' '),' article-body ')]"
             " | //*[contains(concat(' ',normalize-space(@class),' '),' detail-content ')]"
             " | //*[contains(concat(' ',normalize-space(@class),' '),' news-body ')]")
    candidates = tree.xpath(custom or known)
    method = 'configured_xpath' if custom else 'known_container'
    if not candidates and not custom:
        candidates = tree.xpath('//article | //main')
        method = 'semantic_container'
    if not candidates and not custom:
        candidates = tree.xpath('//div[p] | //section[p]')
        method = 'heuristic'

    def score(node):
        text = clean_node(node)
        link_chars = sum(len(normalize_text(' '.join(a.itertext()))) for a in node.xpath('.//a'))
        return len(text) - 2 * link_chars

    candidates = [n for n in candidates if hasattr(n, 'itertext')]
    best = max(candidates, key=score, default=None)
    if best is None:
        return None
    content = clean_node(best)
    text_len = max(1, len(content))
    link_chars = sum(len(normalize_text(' '.join(a.itertext()))) for a in best.xpath('.//a'))
    evidence = (bool(custom) or method == 'known_container' or meta.get('is_article')
                or meta.get('og:type') == 'article'
                or (bool(tree.xpath('//h1')) and len(best.xpath('.//p')) >= 2))
    if not title or len(content) < min_chars or not evidence or link_chars / text_len > .55:
        return None
    return {'title': normalize_text(title), 'content': content,
            'published_at': published or None, 'extraction_method': method,
            'kind': 'article', 'file_type': 'html', 'olinks': outgoing_links(tree, url)}


def sitemap_links(body: bytes) -> list[str]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(body, parser=parser)
    if etree.QName(root).localname not in ('sitemapindex', 'urlset', 'rss', 'feed'):
        return []
    if etree.QName(root).localname in ('sitemapindex', 'urlset'):
        return root.xpath('//*[local-name()="loc"]/text()')
    # Support RSS and Atom feeds when discovered in HTML or explicitly seeded.
    return root.xpath('//*[local-name()="item"]/*[local-name()="link"]/text()'
                      ' | //*[local-name()="entry"]/*[local-name()="link" and (not(@rel) or @rel="alternate")]/@href')


def extract_document(body: bytes, url: str, mime: str, tika_url: str | None, timeout: float) -> dict:
    metadata = {}
    if tika_url:
        import requests
        response = requests.put(tika_url.rstrip('/') + '/rmeta/text', data=body,
                                headers={'Accept': 'application/json', 'Content-Type': mime or 'application/octet-stream'},
                                timeout=timeout)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise ValueError('Tika returned no metadata')
        errors = [str(v) for row in rows for k, v in row.items() if k.startswith('X-TIKA:EXCEPTION')]
        if errors:
            raise ValueError('Tika extraction failed: ' + '; '.join(errors)[:300])
        metadata = rows[0]
        content = '\n'.join(str(row.get('X-TIKA:content', '')) for row in rows)
        method = 'tika'
    elif mime == 'application/pdf' or urlsplit(url).path.lower().endswith('.pdf'):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(body))
        content = '\n'.join(page.extract_text() or '' for page in reader.pages)
        method = 'pypdf_fallback'
    elif mime.startswith('text/plain') or urlsplit(url).path.lower().endswith('.txt'):
        content = body.decode('utf-8-sig', errors='replace')
        method = 'plain_text'
    else:
        raise ValueError('Document saved; configure --tika-url and run reprocess to extract Office files')
    content = normalize_text(content)
    if len(content) < 20:
        raise ValueError('No usable text; scanned/encrypted documents may require OCR or credentials')
    title = metadata.get('dc:title') or metadata.get('title') or PurePosixPath(urlsplit(url).path).name
    if isinstance(title, list):
        title = title[0] if title else ''
    ext = PurePosixPath(urlsplit(url).path.lower()).suffix.lstrip('.')
    return {'title': normalize_text(str(title)), 'content': content,
            'published_at': None, 'extraction_method': method, 'kind': 'document',
            'file_type': ext or mime, 'metadata': metadata, 'olinks': []}

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hust_collect.__main__ import Crawler, FetchError, Store, canonical_url, export_data, parser, seed_reference
from hust_collect.processing import extract_html, fold_text, outgoing_links, sitemap_links, tree_for


def article(text='Thông báo học bổng dành cho sinh viên. ' * 12, links=''):
    return (f'<html><head><meta charset="utf-8"></head><body><nav>MENU KHÔNG LẤY</nav>'
            f'<h1>Học bổng Đại học Bách khoa</h1><time datetime="2026-11-01"></time>'
            f'<div id="bodytext"><p>{text}</p></div>{links}<footer>FOOTER</footer></body></html>').encode()


class Response:
    def __init__(self, status=200, body=b'', headers=None):
        self.status_code, self.body = status, body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield self.body


class Session:
    def __init__(self, routes):
        self.routes, self.headers, self.calls = routes, {}, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.routes.get(url, Response(404))
        if isinstance(result, list):
            return result.pop(0)
        return result


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.args = parser().parse_args(['crawl', '--delay', '0', '--extractors', '', '--max-pages', '8'])

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def row(self, url):
        return self.store.db.execute('SELECT * FROM urls WHERE url=?', (url,)).fetchone()

    def test_scope_and_normalization(self):
        for url in ('https://hust.edu.vn.evil.test/', 'https://evilhust.edu.vn/',
                    'https://hust.edu.vn@evil.test/', 'https://*.hust.edu.vn/',
                    'https://hust.edu.vn:8080/', 'javascript:alert(1)', 'https://hust.edu.vn/a.png'):
            self.assertIsNone(canonical_url(url), url)
        self.assertEqual(canonical_url('/tin?page=2&utm_source=foo#frag', 'https://A.HUST.EDU.VN/'),
                         'https://a.hust.edu.vn/tin?page=2')

    def test_new_only_skips_revisits_and_retries(self):
        crawler = Crawler(self.store, self.args, Session({}))
        urls = ['https://hust.edu.vn/pending', 'https://hust.edu.vn/fetched',
                'https://hust.edu.vn/retry', 'https://other.hust.edu.vn/fetched']
        for url in urls:
            crawler.enqueue(url)
        for url, state in zip(urls[1:], ['fetched', 'retry', 'fetched']):
            self.store.update(url, state=state, next_fetch=0)
        self.args.new_only = True
        with patch.object(crawler, 'visit', return_value='new') as visit:
            self.assertEqual(crawler.run(), {'new': 1})
            self.assertEqual([call.args[0]['url'] for call in visit.call_args_list], [urls[0]])
        self.args.new_only = False
        with patch.object(crawler, 'visit', return_value='unchanged') as visit:
            self.assertEqual(crawler.run(), {'unchanged': 4})
            self.assertEqual(visit.call_count, 4)

    def test_robots_failure_cooldown_and_recovery(self):
        origin = 'https://hust.edu.vn'
        session = Session({origin + '/robots.txt': [
            Response(503), Response(body=b'User-agent: *\nDisallow: /private/')]})
        crawler = Crawler(self.store, self.args, session)
        with patch('hust_collect.__main__.time.monotonic', return_value=100):
            with self.assertRaises(FetchError):
                crawler.rules(origin + '/a')
            with self.assertRaises(FetchError):
                crawler.rules(origin + '/b')
            self.assertEqual(len(session.calls), 1)
            crawler.rules('https://other.hust.edu.vn/a')
            self.assertEqual(len(session.calls), 2)
        with patch('hust_collect.__main__.time.monotonic', return_value=401):
            rules = crawler.rules(origin + '/a')
            self.assertFalse(rules.can_fetch(self.args.user_agent, origin + '/private/a'))
            self.assertEqual(len(session.calls), 3)
            self.assertNotIn(origin, crawler.robots_failures)

    def test_robots_connection_failure_is_cached(self):
        import requests
        crawler = Crawler(self.store, self.args, Session({}))
        with patch.object(crawler.session, 'get', side_effect=requests.ConnectionError('offline')) as get:
            with self.assertRaises(requests.ConnectionError):
                crawler.rules('https://hust.edu.vn/a')
            with self.assertRaises(FetchError):
                crawler.rules('https://hust.edu.vn/b')
            self.assertEqual(get.call_count, 1)

    def test_article_cleaning_and_unicode(self):
        result = extract_html(article(), 'https://hust.edu.vn/a', {}, 200)
        self.assertIn('học bổng', result['content'])
        self.assertNotIn('MENU', result['content'])
        self.assertNotIn('FOOTER', result['content'])
        self.assertEqual(result['published_at'], '2026-11-01')
        # Missing charset must not corrupt UTF-8 Vietnamese.
        bare = article().replace(b'<meta charset="utf-8">', b'')
        self.assertIn('học bổng', extract_html(bare, 'https://hust.edu.vn/a', {}, 200)['content'])
        self.assertEqual(fold_text('Đại học'), 'dai hoc')

    def test_listing_is_not_article(self):
        listing = ('<main><h1>Danh mục</h1>' + '<a href="/a">Tin tuyển sinh mới nhất</a>' * 50 + '</main>').encode()
        self.assertIsNone(extract_html(listing, 'https://hust.edu.vn/', {}, 200))

    def test_hust_news_bodyhtml_container_without_paragraphs(self):
        body = ('<html><h1>Thông tin tuyển sinh</h1>'
                '<div id="news-bodyhtml" class="bodytext margin-bottom-lg">'
                + 'Thông tin tuyển sinh dành cho sinh viên.<br>' * 15
                + '</div><div class="other-news">Những tin mới hơn</div></html>').encode()
        record = extract_html(body, 'https://hust.edu.vn/vi/news/a.html', {}, 200)
        self.assertIsNotNone(record)
        self.assertNotIn('Những tin mới hơn', record['content'])
        self.assertEqual(record['extraction_method'], 'known_container')

    def test_sitemap_and_feed(self):
        self.assertEqual(sitemap_links(b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.hust.edu.vn/a</loc></url></urlset>'),
                         ['https://a.hust.edu.vn/a'])
        self.assertEqual(sitemap_links(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="https://hust.edu.vn/a"/></entry></feed>'),
                         ['https://hust.edu.vn/a'])

    def test_incremental_updates_and_304(self):
        url = 'https://hust.edu.vn/a'
        session = Session({url: [Response(body=article(), headers={'Content-Type': 'text/html; charset=utf-8', 'ETag': 'v1'}),
                                 Response(304),
                                 Response(body=article('Nội dung đã thay đổi về tuyển sinh. ' * 15), headers={'Content-Type': 'text/html', 'ETag': 'v2'})]})
        crawler = Crawler(self.store, self.args, session)
        crawler.enqueue(url)
        self.assertEqual(crawler.visit(self.row(url)), 'new')
        old_hash = self.store.db.execute('SELECT content_hash FROM documents').fetchone()[0]
        self.assertEqual(crawler.visit(self.row(url)), 'not_modified')
        self.assertEqual(session.calls[-1][1]['headers']['If-None-Match'], 'v1')
        self.assertEqual(crawler.visit(self.row(url)), 'updated')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 1)
        self.assertNotEqual(old_hash, self.store.db.execute('SELECT content_hash FROM documents').fetchone()[0])
        output = export_data(self.store)
        record = json.loads(Path(output['output']).read_text(encoding='utf-8'))
        self.assertEqual(record['segmentation'], 'not_applied')

    def test_new_domain_sitemap_and_persistent_resume(self):
        session = Session({
            'https://hust.edu.vn/robots.txt': Response(body=b'User-agent: *\nSitemap: https://hust.edu.vn/map.xml'),
            'https://hust.edu.vn/map.xml': Response(body=b'<urlset><url><loc>https://b.hust.edu.vn/tin</loc></url></urlset>', headers={'Content-Type': 'application/xml'}),
            'https://hust.edu.vn/': Response(body=article(links='<a href="https://a.hust.edu.vn/tin">Tin</a>'), headers={'Content-Type': 'text/html'}),
            'https://a.hust.edu.vn/': Response(body=article(), headers={'Content-Type': 'text/html'}),
            'https://b.hust.edu.vn/tin': Response(body=article(), headers={'Content-Type': 'text/html'}),
        })
        crawler = Crawler(self.store, self.args, session)
        crawler.enqueue('https://hust.edu.vn/')
        crawler.run()
        hosts = {r[0] for r in self.store.db.execute('SELECT host FROM domains')}
        self.assertIn('a.hust.edu.vn', hosts)
        self.assertIn('b.hust.edu.vn', hosts)
        calls = len(session.calls)
        Crawler(self.store, self.args, session).run()
        self.assertEqual(len(session.calls), calls)

    def test_redirect_scope_and_destination_robots(self):
        url = 'https://hust.edu.vn/a'
        session = Session({url: Response(302, headers={'Location': 'https://example.com/private'})})
        crawler = Crawler(self.store, self.args, session)
        with self.assertRaises(FetchError):
            crawler.request(url)
        self.assertFalse(any('example.com' in u for u, _ in session.calls))
        session.routes[url] = Response(302, headers={'Location': 'https://a.hust.edu.vn/blocked'})
        session.routes['https://a.hust.edu.vn/robots.txt'] = Response(body=b'User-agent: *\nDisallow: /blocked')
        with self.assertRaises(FetchError):
            crawler.request(url)
        self.assertFalse(any(u.endswith('/blocked') for u, _ in session.calls))

    def test_parse_failure_keeps_raw_and_can_reprocess(self):
        url = 'https://hust.edu.vn/a.docx'
        session = Session({url: [Response(body=b'Office bytes', headers={'Content-Type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'ETag': 'v1'}), Response(304)]})
        crawler = Crawler(self.store, self.args, session)
        crawler.enqueue(url)
        self.assertEqual(crawler.visit(self.row(url)), 'parse_error')
        self.assertTrue((self.store.directory / self.row(url)['raw_path']).exists())
        with patch('hust_collect.__main__.extract_document', return_value={
                'title': 'Document', 'content': 'Nội dung tài liệu đủ dài để đánh chỉ mục',
                'kind': 'document', 'file_type': 'docx'}):
            self.assertEqual(crawler.visit(self.row(url)), 'not_modified')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 1)

    def test_temporary_errors_preserve_existing_document(self):
        url = 'https://hust.edu.vn/a'
        session = Session({url: [Response(body=article(), headers={'Content-Type': 'text/html'}), Response(503), Response(404), Response(404), Response(404)]})
        crawler = Crawler(self.store, self.args, session)
        crawler.enqueue(url)
        crawler.visit(self.row(url))
        self.assertEqual(crawler.visit(self.row(url)), 'retry')
        for _ in range(2):
            crawler.visit(self.row(url))
            self.assertEqual(self.store.db.execute('SELECT active FROM documents').fetchone()[0], 1)
        crawler.visit(self.row(url))
        self.assertEqual(self.store.db.execute('SELECT active FROM documents').fetchone()[0], 0)

    def test_reprocess_has_offline_frontier_defaults(self):
        args = parser().parse_args(['reprocess', '--extractors', ''])
        crawler = Crawler(self.store, args)
        self.store.enqueue('https://hust.edu.vn/a')
        crawler.process(self.row('https://hust.edu.vn/a'), article(links='<a href="/b">Tin khác</a>'), 'text/html', 'https://hust.edu.vn/a')
        self.assertIsNotNone(self.row('https://hust.edu.vn/b'))

    def test_outlinks_preserve_external_evidence_without_scheduling(self):
        body = article(links='<p>Xem <a href="https://example.org/a">tài liệu</a> để biết thêm.</p>'
                       '<a href="/b#section">Nội bộ</a><a href="mailto:test@example.org">Mail</a>')
        links = outgoing_links(tree_for(body), 'https://hust.edu.vn/a')
        self.assertEqual(links[0]['atext'], 'tài liệu')
        self.assertEqual(links[0]['etext'], 'Xem tài liệu để biết thêm.')
        self.assertEqual(links[1]['dest'], 'https://hust.edu.vn/b#section')
        self.assertEqual(len(links), 2)
        self.store.enqueue('https://hust.edu.vn/a')
        Crawler(self.store, self.args).process(self.row('https://hust.edu.vn/a'), body, 'text/html', 'https://hust.edu.vn/a')
        self.assertIsNone(self.row('https://example.org/a'))

    def test_reference_schema_export_and_seed_without_copying_content(self):
        reference = Path(self.tmp.name) / 'reference.json'
        reference.write_text(json.dumps([{
            'url': 'https://hust.edu.vn/a', 'title': 'OLD TITLE', 'content': 'OLD CONTENT',
            'olinks': [None, {'atext': 'Trường', 'dest': 'https://a.hust.edu.vn/vi/', 'etext': 'Trường'},
                       {'atext': 'External', 'dest': 'https://example.org', 'etext': ''}]
        }]), encoding='utf-8')
        crawler = Crawler(self.store, self.args)
        seed_reference(crawler, reference)
        self.assertIsNotNone(self.row('https://hust.edu.vn/a'))
        self.assertIsNotNone(self.row('https://a.hust.edu.vn/'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 0)
        crawler.process(self.row('https://hust.edu.vn/a'), article(links='<a href="/b">Bài khác</a>'),
                        'text/html', 'https://hust.edu.vn/a')
        result = export_data(self.store)
        exported = json.loads(Path(result['sample_format_output']).read_text(encoding='utf-8'))
        self.assertEqual(list(exported[0]), ['url', 'title', 'content', 'olinks'])
        self.assertEqual(list(exported[0]['olinks'][0]), ['atext', 'dest', 'etext'])
        self.assertNotEqual(exported[0]['content'], 'OLD CONTENT')

    def test_sample_export_uses_final_url_and_merges_redirect_aliases(self):
        record = {'title': 'Bài viết', 'content': 'Nội dung thật', 'olinks': [],
                  'final_url': 'https://hust.edu.vn/new'}
        self.store.document('https://hust.edu.vn/old-a', dict(record))
        self.store.document('https://hust.edu.vn/old-b', dict(record))
        result = export_data(self.store)
        rows = json.loads(Path(result['sample_format_output']).read_text(encoding='utf-8'))
        self.assertEqual(result['documents'], 2)
        self.assertEqual(result['sample_format_documents'], 1)
        self.assertEqual(rows[0]['url'], 'https://hust.edu.vn/new')


if __name__ == '__main__':
    unittest.main()

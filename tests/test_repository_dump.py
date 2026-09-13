import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('repository_dump', Path(__file__).parents[1] / 'scripts/repository_dump.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class Response(io.BytesIO):
    def __init__(self, body=b'example', content_type='application/pdf', length=None):
        super().__init__(body)
        self.headers = {'Content-Type': content_type, 'Content-Length': str(len(body) if length is None else length)}


class StubClient:
    def __init__(self):
        self.calls, self.downloads = [], 0

    def pages(self, url):
        self.calls.append(url)
        if '/issues?' in url:
            return [{'number': 1, 'title': 'closed issue', 'state': 'closed', 'body': '![asset](https://github.com/user-attachments/assets/11111111-1111-4111-8111-111111111111)'}]
        if '/issues/comments?' in url:
            return [{'id': 3, 'issue_url': 'https://api.github.com/repos/o/r/issues/1', 'body': 'complete comment'}]
        if '/pulls?' in url:
            return [{'number': 2, 'body': 'closed PR', 'state': 'closed'}]
        if '/releases?' in url:
            return [{'id': 4, 'body': 'release note'}]
        if '/releases/4/assets?' in url:
            return [{'id': 5, 'url': 'https://api.github.com/repos/o/r/releases/assets/5', 'name': 'program.zip'}]
        return []

    def open(self, url, binary=False):
        self.downloads += 1
        return Response()


class DumpTests(unittest.TestCase):
    def test_pagination_follows_every_page(self):
        client = mod.Client()
        pages = {
            'https://api.github.com/repos/o/r/issues?per_page=100': ([{'id': 1}], {'Link': '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"'}),
            'https://api.github.com/repos/o/r/issues?page=2': ([{'id': 2}], {})}
        client.json = lambda url: pages[url]
        self.assertEqual(client.pages(next(iter(pages))), [{'id': 1}, {'id': 2}])

    def test_pagination_rejects_changed_host(self):
        client = mod.Client()
        client.json = lambda url: ([], {'Link': '<https://example.com/steal>; rel="next"'})
        with self.assertRaises(ValueError):
            client.pages('https://api.github.com/repos/o/r/issues?per_page=100')

    def test_github_numeric_repository_pagination(self):
        client = mod.Client()
        client.json = lambda url: ([{'id': 2}], {}) if '/repositories/' in url else ([{'id': 1}], {'Link': '<https://api.github.com/repositories/1351162362/issues/comments?page=2>; rel="next"'})
        self.assertEqual(client.pages('https://api.github.com/repos/o/r/issues/comments?per_page=100'), [{'id': 1}, {'id': 2}])

    def test_github_storage_redirect_strips_token(self):
        from urllib.request import Request
        request = Request('https://api.github.com/repos/o/r/releases/assets/1', headers={'Authorization': 'Bearer example'})
        redirect = mod.DownloadRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://github-production-user-asset-6210df.s3.amazonaws.com/example')
        self.assertIsNone(redirect.get_header('Authorization'))
        with self.assertRaises(ValueError):
            mod.DownloadRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://unrelated-bucket.s3.amazonaws.com/example')

    def test_source_archive_redirect_strips_api_token(self):
        from urllib.request import Request
        request = Request('https://api.github.com/repos/o/r/zipball/v1', headers={'Authorization': 'Bearer example'})
        redirect = mod.DownloadRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://codeload.github.com/o/r/legacy.zip/v1')
        self.assertIsNone(redirect.get_header('Authorization'))
        self.assertFalse(mod.permitted_download('https://codeload.github.com.evil.test/o/r/zip/v1'))

    def test_source_archive_request_uses_api_media_type(self):
        class HeaderCheckingOpener:
            def open(self, request, timeout):
                if '/releases/assets/' in request.full_url:
                    expected = 'application/octet-stream'
                else:
                    expected = 'application/vnd.github+json'
                if request.get_header('Accept') != expected:
                    from urllib.error import HTTPError
                    raise HTTPError(request.full_url, 415, 'Unsupported Media Type', {}, None)
                return Response()
        client = mod.Client()
        client.opener = HeaderCheckingOpener()
        for endpoint in ('zipball/v1', 'tarball/v1', 'releases/assets/123'):
            with client.open('https://api.github.com/repos/o/r/' + endpoint, binary=True) as response:
                self.assertEqual(response.read(), b'example')

    def test_release_source_archives_are_saved_and_failure_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = StubClient()
            original = client.pages
            archives = {'zipball_url': 'https://api.github.com/repos/o/r/zipball/v1',
                        'tarball_url': 'https://api.github.com/repos/o/r/tarball/v1'}
            client.pages = lambda url: [dict(id=4, body='release', **archives)] if '/releases?' in url else original(url)
            result = mod.Archive(client, 'o/r', tmp).run()
            self.assertTrue(result['complete'])
            for field, url in archives.items():
                self.assertIn(url, result['assets'])
                self.assertIn(f'releases.json#4/{field}', result['assets'][url]['referenced_by'])
            self.assertEqual(client.downloads, 4)
        with tempfile.TemporaryDirectory() as tmp:
            original_open = client.open
            def failed_source(url, binary=False):
                if '/tarball/' in url:
                    raise OSError('archive unavailable')
                return original_open(url, binary)
            client.open = failed_source
            result = mod.Archive(client, 'o/r', tmp).run()
            self.assertFalse(result['complete'])
            self.assertTrue(any(f['resource'] == archives['tarball_url'] for f in result['failures']))

    def test_uploaded_markdown_html_bare_and_legacy_links(self):
        data = {'body': '![a](https://github.com/user-attachments/assets/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa) <img src="https://github.com/user-attachments/assets/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" />\nhttps://github.com/o/r/files/55/a.pdf\nhttps://example.com/no.jpg'}
        self.assertEqual(len(mod.attachments(data)), 3)

    def test_url_code_fragments_do_not_discard_a_collection(self):
        self.assertEqual(mod.attachments({'patch': 'url = https://[host\n![ok](https://github.com/user-attachments/assets/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa)'}), {'https://github.com/user-attachments/assets/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'})

    def test_inline_code_delimiter_is_not_part_of_attachment_url(self):
        url = 'https://github.com/user-attachments/assets/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        self.assertEqual(mod.attachments('Download `' + url + '`.'), {url})

    def test_code_patch_and_upload_prefix_are_not_real_attachments(self):
        self.assertEqual(mod.attachments('https://github.com/user-attachments/'), set())
        with tempfile.TemporaryDirectory() as tmp:
            client = StubClient()
            client.pages = lambda url: [{'body': 'text', 'patch': 'https://github.com/user-attachments/assets/code-example'}]
            archive = mod.Archive(client, 'o/r', tmp)
            archive.collection('/pulls/1/files', 'files.json')
            self.assertEqual(archive.refs, {})

    def test_placeholder_asset_id_is_not_a_download(self):
        self.assertEqual(mod.attachments('e.g. `https://github.com/user-attachments/assets/xxxx`'), set())

    def test_legacy_uploaded_images_and_repo_assets(self):
        urls = {'https://user-images.githubusercontent.com/123/456-example.png',
                'https://github.com/o/r/assets/123/11111111-1111-4111-8111-111111111111'}
        self.assertEqual(mod.attachments('\n'.join(urls)), urls)

    def test_all_conversations_release_assets_and_cached_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = StubClient()
            first = mod.Archive(client, 'o/r', tmp).run()
            self.assertTrue(first['complete'])
            for suffix in ('reviews', 'comments', 'commits', 'files'):
                self.assertTrue(any('/pulls/2/' + suffix in u for u in client.calls))
            self.assertEqual(len(first['assets']), 2)
            self.assertEqual(client.downloads, 2)
            self.assertIn('complete comment', (Path(tmp) / 'readable/1.md').read_text('utf-8'))
            again = mod.Archive(client, 'o/r', tmp).run()
            self.assertTrue(again['complete'])
            self.assertEqual(client.downloads, 2)
            asset = next(iter(again['assets'].values()))
            (Path(tmp) / asset['path']).write_bytes(b'corrupted')
            mod.Archive(client, 'o/r', tmp).run()
            self.assertEqual(client.downloads, 3)

    def test_collection_error_is_incomplete_not_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = StubClient()
            client.pages = lambda url: (_ for _ in ()).throw(OSError('offline'))
            result = mod.Archive(client, 'o/r', tmp).run()
            self.assertFalse(result['complete'])
            self.assertGreater(len(result['failures']), 0)

    def test_uploaded_html_document_is_saved_when_served_as_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = StubClient()
            def open_html(url, binary=False):
                response = Response(b'<!doctype html><title>Document</title>', 'text/html')
                response.headers['Content-Disposition'] = 'attachment;filename=document.html'
                return response
            client.open = open_html
            archive = mod.Archive(client, 'o/r', tmp)
            url = 'https://github.com/user-attachments/files/123/document.html'
            archive.refs[url] = ['issues.json']
            archive.asset(url)
            self.assertEqual(archive.failures, [])
            self.assertEqual((Path(tmp) / archive.assets[url]['path']).read_bytes(), b'<!doctype html><title>Document</title>')

    def test_truncated_or_html_downloads_are_not_complete(self):
        for response in [lambda: Response(length=999), lambda: Response(content_type='text/html')]:
            with tempfile.TemporaryDirectory() as tmp:
                client = StubClient()
                client.open = lambda *a, **k: response()
                result = mod.Archive(client, 'o/r', tmp).run()
                self.assertFalse(result['complete'])
                self.assertEqual(result['assets'], {})
                self.assertEqual(list(Path(tmp).rglob('*.part')), [])

    def test_rejects_non_github_downloads(self):
        for url in ('http://github.com/a', 'https://127.0.0.1/a', 'https://github.com.evil.test/a', 'https://u:p@github.com/a'):
            self.assertFalse(mod.permitted_download(url))


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Archive GitHub repository conversations and uploaded assets (standard library only)."""
import argparse
import hashlib
import html
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlsplit, unquote, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler


def permitted_download(url):
    p = urlsplit(url)
    host = p.hostname or ''
    return (p.scheme == 'https' and not p.username and not p.password
            and p.port in (None, 443) and (host in ('github.com', 'api.github.com',
                'github-production-user-asset-6210df.s3.amazonaws.com')
            or host.endswith('.githubusercontent.com')))


class DownloadRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not permitted_download(newurl):
            raise ValueError('Download redirected outside GitHub hosting')
        nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
        # Never send API credentials to an attachment CDN, including on redirect.
        if urlsplit(newurl).hostname != 'api.github.com':
            nxt.remove_header('Authorization')
        return nxt


class Client:
    def __init__(self, token=None):
        self.token = token
        self.opener = build_opener(DownloadRedirect())

    def open(self, url, binary=False):
        if not permitted_download(url):
            raise ValueError('Not a GitHub HTTPS resource')
        headers = {'User-Agent': 'repository-dump/1.0'}
        if urlsplit(url).hostname == 'api.github.com':
            headers.update({'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json',
                            'X-GitHub-Api-Version': '2022-11-28'})
            if self.token:
                headers['Authorization'] = 'Bearer ' + self.token
        for attempt in range(3):
            try:
                return self.opener.open(Request(url, headers=headers), timeout=60)
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                delay = exc.headers.get('Retry-After', '')
                time.sleep(min(int(delay), 30) if delay.isdigit() else 2 ** attempt)

    def json(self, url):
        with self.open(url) as response:
            return json.load(response), response.headers

    def pages(self, url):
        rows, visited = [], set()
        prefix = url.split('?', 1)[0]
        # GitHub's Link headers canonicalize /repos/OWNER/NAME to /repositories/ID.
        suffix = re.sub(r'^https://api\.github\.com/repos/[^/]+/[^/]+', '', prefix)
        canonical = re.compile(r'https://api\.github\.com/repositories/[0-9]+' + re.escape(suffix) + r'$')
        while url:
            page_path = url.split('?', 1)[0]
            if url in visited or (page_path != prefix and not canonical.fullmatch(page_path)):
                raise ValueError('Invalid pagination link')
            visited.add(url)
            data, headers = self.json(url)
            if not isinstance(data, list):
                raise ValueError('Expected a paginated array')
            rows.extend(data)
            links = re.findall(r'<([^>]+)>;\s*rel="next"', headers.get('Link', ''))
            url = links[0] if links else None
        return rows


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def attachments(value):
    found = set()
    asset_id = r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}'
    for text in strings(value):
        for raw in re.findall(r'https://[^\s<>"`\u201c\u201d]+', html.unescape(text)):
            url = raw.rstrip(').,;!\'')
            try:
                p = urlsplit(url)
            except ValueError:
                # Source code in PR diffs can contain incomplete URL expressions.
                continue
            uploaded = p.hostname == 'github.com' and (
                re.fullmatch(r'/user-attachments/assets/' + asset_id, p.path)
                or re.fullmatch(r'/user-attachments/files/[0-9]+/[^/]+', p.path)
                or re.fullmatch(r'/[^/]+/[^/]+/assets/[0-9]+/' + asset_id, p.path)
                or re.fullmatch(r'/[^/]+/[^/]+/files/[0-9]+/[^/]+', p.path))
            legacy_image = p.hostname == 'user-images.githubusercontent.com' and re.fullmatch(r'/[0-9]+/[^/]+', p.path)
            if uploaded or legacy_image:
                found.add(url)
    return found


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


class Archive:
    def __init__(self, client, repository, output):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Repository must be owner/name')
        self.client, self.repository, self.output = client, repository, Path(output).resolve()
        self.api = 'https://api.github.com/repos/' + repository
        self.output.mkdir(parents=True, exist_ok=True)
        self.failures, self.assets, self.refs, self.counts = [], {}, {}, {}
        old = self.output / 'manifest.json'
        self.cache = json.loads(old.read_text(encoding='utf-8')).get('assets', {}) if old.exists() else {}

    def collection(self, endpoint, filename):
        url = self.api + endpoint + ('&' if '?' in endpoint else '?') + 'per_page=100'
        try:
            rows = self.client.pages(url)
            save_json(self.output / filename, rows)
            self.counts[filename] = len(rows)
            # Scan conversation text, not code patches containing example URL literals.
            for asset in attachments([row.get('body') for row in rows if isinstance(row, dict)]):
                self.refs.setdefault(asset, []).append(filename)
            return rows
        except Exception as exc:
            self.failures.append({'resource': endpoint, 'error': type(exc).__name__ + ': ' + str(exc)})
            print(f'Collection failed: {endpoint}: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
            return []

    def asset(self, url):
        cached = self.cache.get(url, {})
        path = (self.output / cached.get('path', '__absent__')).resolve()
        if (path.is_relative_to(self.output) and path.is_file()
                and cached.get('sha256') == digest(path)):
            self.assets[url] = dict(cached, referenced_by=sorted(set(self.refs[url])))
            return
        prefix = hashlib.sha256(url.encode()).hexdigest()
        tmp = self.output / 'assets' / (prefix + '.part')
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.client.open(url, binary=True) as response, tmp.open('wb') as stream:
                kind = response.headers.get('Content-Type', '').split(';')[0]
                disposition = response.headers.get('Content-Disposition', '').split(';', 1)[0].strip().lower()
                expected = response.headers.get('Content-Length')
                size = 0
                for chunk in iter(lambda: response.read(1024 * 1024), b''):
                    stream.write(chunk)
                    size += len(chunk)
            if expected and expected.isdigit() and size != int(expected):
                raise ValueError('Truncated download')
            if kind == 'text/html' and disposition != 'attachment':
                raise ValueError('Attachment returned an HTML page instead of a file')
            original = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
            suffix = Path(original).suffix
            if not re.fullmatch(r'\.[A-Za-z0-9]{1,10}', suffix):
                suffix = mimetypes.guess_extension(kind) or '.bin'
            target = tmp.with_name(prefix + suffix)
            tmp.replace(target)
            self.assets[url] = {'path': target.relative_to(self.output).as_posix(), 'original_name': original,
                                'bytes': size, 'sha256': digest(target), 'content_type': kind,
                                'referenced_by': sorted(set(self.refs[url]))}
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            self.failures.append({'resource': url, 'error': type(exc).__name__ + ': ' + str(exc)})
            print(f'Attachment failed: {url}: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)

    def run(self):
        issue_rows = self.collection('/issues?state=all&sort=created&direction=asc', 'issues-and-prs.json')
        comments = self.collection('/issues/comments', 'issue-comments.json')
        pulls = self.collection('/pulls?state=all&sort=created&direction=asc', 'pulls.json')
        for pr in pulls:
            n = int(pr['number'])
            for endpoint in ('reviews', 'comments', 'commits', 'files'):
                self.collection(f'/pulls/{n}/{endpoint}', f'pulls/{n}/{endpoint}.json')
        releases = self.collection('/releases', 'releases.json')
        self.collection('/tags', 'tags.json')
        for release in releases:
            rid = int(release['id'])
            assets = self.collection(f'/releases/{rid}/assets', f'releases/{rid}/assets.json')
            for asset in assets:
                # API URL supports private release assets with the Actions token.
                url = asset['url']
                self.refs.setdefault(url, []).append(f'releases/{rid}/assets.json')
        for index, url in enumerate(sorted(self.refs), 1):
            self.asset(url)
            save_json(self.output / 'manifest.json', {'schema': 1, 'repository': self.repository,
                      'complete': False, 'running': True, 'counts': self.counts,
                      'assets': self.assets, 'failures': self.failures})
            print(f'Attachment {index}/{len(self.refs)}', flush=True)
        manifest = {'schema': 1, 'repository': self.repository,
                    'started_scope': 'All states: issues, issue/PR comments, PR reviews/inline comments/commits/files, releases/assets, tags, GitHub-uploaded attachments.',
                    'checked_at': datetime.now(timezone.utc).isoformat(), 'complete': not self.failures,
                    'counts': self.counts, 'assets': self.assets, 'failures': self.failures}
        save_json(self.output / 'manifest.json', manifest)
        rows = ['# Repository archive', '', self.repository, '',
                'Status: ' + ('COMPLETE' if manifest['complete'] else 'INCOMPLETE — inspect manifest.json'), '',
                '[Raw manifest and SHA-256 checksums](manifest.json)', '',
                f'Issues and PRs: {len(issue_rows)}; pull requests: {len(pulls)}; releases: {len(releases)};',
                f'downloaded assets: {len(self.assets)}; failures: {len(self.failures)}.', '',
                '## Conversations', '']
        for item in issue_rows:
            n = int(item['number'])
            text = f"# {item.get('title', '')}\n\nSource: {item.get('html_url', '')}\n\n{item.get('body') or ''}\n"
            for comment in comments:
                if comment.get('issue_url', '').rstrip('/').endswith('/' + str(n)):
                    author = (comment.get('user') or {}).get('login', 'unknown')
                    text += f"\n---\n\n## {author} — {comment.get('created_at', '')}\n\n{comment.get('body') or ''}\n"
            # Original text remains untouched in JSON; this copy links to local attachments.
            for url, info in sorted(self.assets.items(), key=lambda x: len(x[0]), reverse=True):
                text = text.replace(url, '../' + info['path'])
            target = self.output / 'readable' / f'{n}.md'
            target.parent.mkdir(exist_ok=True)
            target.write_text(text, encoding='utf-8')
            title = str(item.get('title', '')).replace('\n', ' ').replace('[', '\\[').replace(']', '\\]')
            rows.append(f'- [#{n} {title}](readable/{n}.md)')
        rows.extend(['', '## Raw collections', ''])
        rows.extend(f'- [{name}]({quote(name)}) ({count})' for name, count in sorted(self.counts.items()))
        (self.output / 'README.md').write_text('\n'.join(rows) + '\n', encoding='utf-8')
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--output', default='repository-archive')
    args = parser.parse_args()
    archive = Archive(Client(os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')),
                      args.repository, args.output)
    manifest = archive.run()
    print(json.dumps({'complete': manifest['complete'], 'counts': manifest['counts'],
                      'assets': len(manifest['assets']), 'failures': len(manifest['failures'])}))
    return 0 if manifest['complete'] else 1


if __name__ == '__main__':
    sys.exit(main())

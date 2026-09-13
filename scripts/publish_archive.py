"""Commit a completed archive to a dedicated branch without modifying the source index."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

MAX_FILE_BYTES = 100 * 1024 * 1024
BRANCH = 'repository-archive'


def publish(repository='.'):
    root = Path(repository).resolve()
    archive = root / 'repository-archive'
    manifest = json.loads((archive / 'manifest.json').read_text(encoding='utf8'))
    if manifest.get('complete') is not True or manifest.get('running'):
        raise ValueError('Archive is incomplete; inspect repository-archive/manifest.json')
    oversized = [str(p.relative_to(root)) for p in archive.rglob('*')
                 if p.is_file() and p.stat().st_size >= MAX_FILE_BYTES]
    if oversized:
        raise ValueError('Files exceed ordinary Git storage limit; downloadable archive retained: ' + ', '.join(oversized))
    environment = dict(os.environ, GIT_AUTHOR_NAME='github-actions[bot]',
                       GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',
                       GIT_COMMITTER_NAME='github-actions[bot]',
                       GIT_COMMITTER_EMAIL='41898282+github-actions[bot]@users.noreply.github.com')

    def git(*args, input=None):
        return subprocess.run(['git', *args], cwd=root, env=environment, input=input,
                              text=True, capture_output=True, check=True).stdout.strip()

    source = git('rev-parse', 'HEAD')
    remote_ref = git('ls-remote', '--heads', 'origin', 'refs/heads/' + BRANCH)
    parent = source
    if remote_ref:
        git('fetch', 'origin', BRANCH)
        parent = git('rev-parse', 'FETCH_HEAD')
    with tempfile.TemporaryDirectory(prefix='repository-archive-index-') as temporary:
        environment['GIT_INDEX_FILE'] = str(Path(temporary) / 'index')
        git('read-tree', source)
        git('add', '--force', '--', 'repository-archive')
        tree = git('write-tree')
        if remote_ref and tree == git('rev-parse', parent + '^{tree}'):
            return parent
        commit = git('commit-tree', tree, '-p', parent,
                     input='Archive repository conversations and uploaded assets\n')
        # A concurrent remote update is rejected by a normal, non-force push.
        git('push', 'origin', commit + ':refs/heads/' + BRANCH)
    return commit


if __name__ == '__main__':
    print(publish())

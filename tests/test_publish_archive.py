import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('publish_archive', Path(__file__).parents[1] / 'scripts/publish_archive.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@unittest.skipUnless(shutil.which('git'), 'Git is required for publication integration tests')
class PublicationTests(unittest.TestCase):
    def test_two_real_pushes_preserve_source_index_and_archive_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, source = root / 'remote.git', root / 'source'
            subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
            subprocess.run(['git', 'init', str(source)], check=True, capture_output=True)

            def git(*args):
                return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args], cwd=source, check=True, text=True, capture_output=True).stdout.strip()

            (source / 'original.txt').write_text('original')
            git('add', 'original.txt')
            git('commit', '-m', 'initial')
            git('remote', 'add', 'origin', str(remote))
            head = git('rev-parse', 'HEAD')
            (source / 'staged.txt').write_text('keep staged')
            git('add', 'staged.txt')
            index_before = git('write-tree')
            output = source / 'repository-archive'
            output.mkdir()
            (output / 'manifest.json').write_text(json.dumps({'complete': True}))
            (output / 'thread.md').write_text('version one')
            first = mod.publish(source)
            (output / 'thread.md').write_text('version two')
            second = mod.publish(source)
            self.assertNotEqual(first, second)
            self.assertEqual(git('rev-parse', second + '^'), first)
            self.assertEqual(git('show', first + ':repository-archive/thread.md'), 'version one')
            self.assertEqual(git('show', second + ':repository-archive/thread.md'), 'version two')
            self.assertEqual(git('rev-parse', 'HEAD'), head)
            self.assertEqual(git('write-tree'), index_before)
            self.assertEqual(mod.publish(source), second)
            (output / 'manifest.json').write_text(json.dumps({'complete': False}))
            with self.assertRaises(ValueError):
                mod.publish(source)
            (output / 'manifest.json').write_text(json.dumps({'complete': True}))
            old_limit = mod.MAX_FILE_BYTES
            try:
                mod.MAX_FILE_BYTES = 2
                with self.assertRaises(ValueError):
                    mod.publish(source)
            finally:
                mod.MAX_FILE_BYTES = old_limit
            self.assertEqual(git('ls-remote', 'origin', 'refs/heads/repository-archive').split()[0], second)


if __name__ == '__main__':
    unittest.main()

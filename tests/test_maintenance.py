import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'local-web'))
from maintenance import BoundedLog, cleanup_versions, process_alive


class MaintenanceTests(unittest.TestCase):
    def test_current_process_is_alive_and_absent_pid_is_not(self):
        self.assertTrue(process_alive(os.getpid()))
        self.assertFalse(process_alive(2147483647))
        self.assertFalse(process_alive(None))

    def test_cleanup_keeps_active_previous_running_and_unknown_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / 'app/versions'
            root.mkdir(parents=True)
            paths = []
            for version in range(6):
                folder = root / f'2.{version}.0-0123456789abcdef'
                folder.mkdir()
                exe = folder / 'SongdoDaily.exe'
                exe.write_bytes(b'program')
                paths.append(exe)
            # Unknown files make a whole cache directory ineligible for deletion.
            (paths[4].parent / 'keep.xlsx').write_bytes(b'never delete')
            unusual = root / 'user-folder'
            unusual.mkdir()
            (unusual / 'settings.json').write_bytes(b'untouched')
            settings = base / 'settings.json'
            settings.write_bytes(b'user settings')
            cleanup_versions(root, paths[:3])
            self.assertTrue(all(path.exists() for path in paths[:3]))
            self.assertFalse(paths[3].parent.exists())
            self.assertTrue(paths[4].exists())
            self.assertFalse(paths[5].parent.exists())
            self.assertEqual(settings.read_bytes(), b'user settings')
            self.assertEqual((unusual / 'settings.json').read_bytes(), b'untouched')

    def test_cleanup_does_not_follow_directory_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, outside = base / 'versions', base / 'outside'
            root.mkdir(); outside.mkdir()
            exe = outside / 'SongdoDaily.exe'
            exe.write_bytes(b'untouched')
            try:
                os.symlink(outside, root / '1.0.0-0123456789abcdef', target_is_directory=True)
            except OSError:
                self.skipTest('Directory symlinks unavailable for this Windows account')
            cleanup_versions(root, [])
            self.assertEqual(exe.read_bytes(), b'untouched')

    def test_cleanup_handles_windows_projected_directory_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'versions';root.mkdir()
            folder=root/'1.0.0-0123456789abcdef';folder.mkdir()
            (folder/'SongdoDaily.exe').write_bytes(b'old program')
            original=Path.resolve
            def projected(path,*args,**kwargs):
                return Path(temporary)/'projected'/path.name if path==folder else original(path,*args,**kwargs)
            with patch.object(Path,'resolve',projected):
                cleanup_versions(root,[])
            self.assertFalse(folder.exists())

    def test_logs_rotate_and_bound_legacy_files_and_large_unicode_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'server.log'
            path.write_bytes(b'old' * 10000)
            log = BoundedLog(path, limit=128, backups=2)
            for index in range(20):
                log.write(f'{index}: request\n' * 5)
            log.write('한글' * 1000)
            files = [p for p in path.parent.iterdir() if not p.name.endswith('.lock')]
            self.assertEqual(len(files), 3)
            self.assertTrue(all(p.stat().st_size <= 128 for p in files))
            for item in files:
                item.read_text(encoding='utf-8')
            self.assertTrue(path.read_text(encoding='utf-8').endswith('한글'))

    def test_concurrent_log_writers_preserve_complete_messages(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'updater.log'
            streams = [BoundedLog(path) for _ in range(4)]
            workers = [threading.Thread(target=lambda i=i: [streams[i].write(f'{i}:{j}\n') for j in range(50)]) for i in range(4)]
            for worker in workers: worker.start()
            for worker in workers: worker.join()
            self.assertEqual(set(path.read_text().splitlines()), {f'{i}:{j}' for i in range(4) for j in range(50)})

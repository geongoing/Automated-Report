import json
from pathlib import Path
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'local-web'))
import persistence as store


def validate(value):
    if not isinstance(value, dict) or type(value.get('revision')) is not int:
        raise ValueError('invalid saved state')
    return value


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.json'

    def test_three_distinct_backups_and_unchanged_healthy_reads(self):
        for revision in range(7):
            store.write_backed_json(self.path, {'revision': revision}, validate)
        raw = self.path.read_bytes()
        for _ in range(4):
            self.assertEqual(store.read_backed_json(self.path, validate, dict), {'revision': 6})
        self.assertEqual(self.path.read_bytes(), raw)
        snapshots = store.snapshots(self.path)
        self.assertEqual(len(snapshots), 3)
        self.assertEqual([json.loads(p.read_bytes())['revision'] for p in snapshots], [6, 5, 4])

    def test_corrupt_primary_skips_a_damaged_backup_and_preserves_original_bytes(self):
        for revision in range(3):
            store.write_backed_json(self.path, {'revision': revision}, validate)
        store.snapshots(self.path)[0].write_bytes(b'broken backup')
        damaged = b'{broken primary'
        self.path.write_bytes(damaged)
        self.assertEqual(store.read_backed_json(self.path, validate, dict), {'revision': 1})
        self.assertEqual(json.loads(self.path.read_bytes()), {'revision': 1})
        self.assertEqual((store.backup_folder(self.path) / 'damaged.json').read_bytes(), damaged)

    def test_missing_file_recovers_but_first_install_stays_empty(self):
        self.assertEqual(store.read_backed_json(self.path, validate, dict), {})
        self.assertFalse(self.path.exists())
        store.write_backed_json(self.path, {'revision': 2}, validate)
        self.path.unlink()
        self.assertEqual(store.read_backed_json(self.path, validate, dict), {'revision': 2})
        self.assertTrue(self.path.exists())

    def test_valid_json_with_invalid_fields_is_not_accepted_as_a_backup(self):
        store.write_backed_json(self.path, {'revision': 1}, validate)
        self.path.write_bytes(b'{"revision": "wrong type"}')
        self.assertEqual(store.read_backed_json(self.path, validate, dict), {'revision': 1})

    def test_failed_primary_write_keeps_old_file_and_a_recovery_copy(self):
        store.write_backed_json(self.path, {'revision': 1}, validate)
        old = self.path.read_bytes()
        write = store.atomic_bytes
        def fail(path, raw):
            if path == self.path: raise OSError('disk write interrupted')
            return write(path, raw)
        with patch.object(store, 'atomic_bytes', side_effect=fail), self.assertRaises(OSError):
            store.write_backed_json(self.path, {'revision': 2}, validate)
        self.assertEqual(self.path.read_bytes(), old)
        self.assertTrue(any(json.loads(p.read_bytes()) == {'revision': 2} for p in store.snapshots(self.path)))

    def test_concurrent_saves_never_share_a_temporary_file(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda revision: store.write_backed_json(self.path, {'revision': revision}, validate), range(16)))
        self.assertIsInstance(json.loads(self.path.read_bytes())['revision'], int)
        self.assertEqual(len(store.snapshots(self.path)), 3)
        self.assertFalse(list(self.path.parent.rglob('*.tmp')))

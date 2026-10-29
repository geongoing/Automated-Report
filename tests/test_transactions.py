import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'local-web'))
import transactions as tx
from fixtures import make_report, TODAY


class Interrupted(BaseException):
    pass


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        pending = patch.object(tx, 'PENDING', self.base / 'pending-writes')
        pending.start(); self.addCleanup(pending.stop)
        final, hmi = self.base / 'final', self.base / 'hmi'
        final.mkdir(); hmi.mkdir()
        self.report = final / '20261027.xlsx'
        self.hmi = hmi / '송도지소_하수_20261026.xlsx'
        self.report_data = make_report(TODAY, True)
        self.hmi_data = (ROOT / '송도_HMI.xlsx').read_bytes()

    def save(self):
        return tx.commit_pair(self.report_data, self.report, self.hmi_data, self.hmi, TODAY.isoformat())

    def interrupt_after_report(self):
        publish = tx.publish_output
        def abrupt(folder, journal, item):
            result = publish(folder, journal, item)
            if item['kind'] == 'report': raise Interrupted()
            return result
        with patch.object(tx, 'publish_output', side_effect=abrupt), self.assertRaises(Interrupted):
            self.save()

    def assert_complete(self):
        self.assertEqual(self.report.read_bytes(), self.report_data)
        self.assertEqual(self.hmi.read_bytes(), self.hmi_data)
        for path in (self.report, self.hmi):
            with zipfile.ZipFile(path) as archive: self.assertIsNone(archive.testzip())
        self.assertFalse(list(tx.PENDING.iterdir()))
        self.assertFalse([p for folder in (self.report.parent,self.hmi.parent) for p in folder.iterdir() if '.songdo-' in p.name])

    def test_interrupted_first_publish_completes_once_without_rewriting_the_report(self):
        self.interrupt_after_report()
        stamp = self.report.stat().st_mtime_ns
        self.assertFalse(self.hmi.exists())
        self.assertFalse(tx.recover_pending())
        self.assert_complete()
        self.assertEqual(self.report.stat().st_mtime_ns, stamp)
        self.assertFalse(tx.recover_pending())
        self.assertEqual(self.report.stat().st_mtime_ns, stamp)

    def test_new_hmi_filename_recovers_with_the_same_schema_as_legacy_records(self):
        self.hmi = self.hmi.parent / '송도_HMI 중앙제어실 _운영일지(10.26).xlsx'
        self.interrupt_after_report()
        stamp = self.report.stat().st_mtime_ns
        self.assertFalse(tx.recover_pending())
        self.assert_complete()
        self.assertEqual(self.report.stat().st_mtime_ns, stamp)
        self.assertFalse(tx.recover_pending())
        self.assertEqual(self.report.stat().st_mtime_ns, stamp)

    def test_partial_staging_file_is_replaced_without_exposing_a_partial_workbook(self):
        publish = tx.publish_output
        def partial(folder, journal, item):
            if item['kind'] == 'hmi':
                tx.stage_path(journal, item).write_bytes(b'half written')
                stage = tx.stage_path(journal, item)
                stage.with_name('.' + stage.name + '.leftover.tmp').write_bytes(b'interrupted sibling')
                raise Interrupted()
            return publish(folder, journal, item)
        with patch.object(tx, 'publish_output', side_effect=partial), self.assertRaises(Interrupted): self.save()
        self.assertFalse(self.hmi.exists())
        self.assertFalse(tx.recover_pending())
        self.assert_complete()

    def test_foreign_or_edited_workbook_is_never_overwritten_during_recovery(self):
        self.interrupt_after_report()
        self.hmi.write_bytes(b'existing user workbook')
        self.assertEqual(len(tx.recover_pending()), 1)
        self.assertEqual(self.hmi.read_bytes(), b'existing user workbook')
        self.assertEqual(self.report.read_bytes(), self.report_data)
        self.assertTrue(list(tx.PENDING.iterdir()))
        self.hmi.unlink()  # Test-owned conflict removed; recovery can resume.
        self.assertFalse(tx.recover_pending())
        self.assert_complete()

    def test_both_payloads_are_checked_before_recovery_publishes_anything(self):
        self.interrupt_after_report()
        folder = next(tx.PENDING.iterdir())
        (folder / 'hmi.xlsx').write_bytes(b'damaged recovery data')
        self.assertEqual(len(tx.recover_pending()), 1)
        self.assertFalse(self.hmi.exists())
        self.assertEqual(self.report.read_bytes(), self.report_data)

    def test_completed_journal_cleanup_preserves_later_user_edits(self):
        with patch.object(tx, 'cleanup', side_effect=Interrupted()), self.assertRaises(Interrupted): self.save()
        self.report.write_bytes(b'user edited completed report')
        self.assertFalse(tx.recover_pending())
        self.assertEqual(self.report.read_bytes(), b'user edited completed report')
        self.assertEqual(self.hmi.read_bytes(), self.hmi_data)

    def test_interrupted_error_rollback_only_removes_unchanged_owned_files(self):
        publish = tx.publish_output
        def fail(folder, journal, item):
            if item['kind'] == 'hmi': raise OSError('disk full')
            return publish(folder, journal, item)
        with patch.object(tx, 'publish_output', side_effect=fail), patch.object(tx, 'rollback', side_effect=Interrupted()), self.assertRaises(Interrupted):
            self.save()
        self.report.write_bytes(b'user changed this file')
        self.assertFalse(tx.recover_pending())
        self.assertEqual(self.report.read_bytes(), b'user changed this file')
        self.assertFalse(self.hmi.exists())

    def test_abrupt_process_exit_recovers_in_a_fresh_process_context(self):
        (self.base / 'input-report.xlsx').write_bytes(self.report_data)
        (self.base / 'input-hmi.xlsx').write_bytes(self.hmi_data)
        script = '''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import transactions as tx
base=Path(sys.argv[2]);tx.PENDING=base/'pending-writes'
publish=tx.publish_output
def abrupt(folder,journal,item):
    result=publish(folder,journal,item)
    if item['kind']=='report':os._exit(73)
    return result
tx.publish_output=abrupt
tx.commit_pair((base/'input-report.xlsx').read_bytes(),Path(sys.argv[3]),(base/'input-hmi.xlsx').read_bytes(),Path(sys.argv[4]),'2026-10-26')
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(ROOT / 'local-web'), str(self.base), str(self.report), str(self.hmi)], timeout=20)
        self.assertEqual(result.returncode, 73)
        self.assertFalse(self.hmi.exists())
        self.assertFalse(tx.recover_pending())
        self.assert_complete()

    def test_error_immediately_after_publication_rolls_back_both_owned_outputs(self):
        sync = tx.sync_directory
        def fail_after_publish(folder):
            if folder == self.hmi.parent and self.hmi.exists():
                raise OSError('share disconnected immediately after rename')
            return sync(folder)
        with patch.object(tx, 'sync_directory', side_effect=fail_after_publish), self.assertRaises(OSError):
            self.save()
        self.assertFalse(self.report.exists())
        self.assertFalse(self.hmi.exists())
        self.assertFalse(list(tx.PENDING.iterdir()))

    def test_ownership_is_durable_before_rename_and_recovery_resumes(self):
        operation = 'rename' if os.name == 'nt' else 'link'
        def abrupt(source, destination):
            folder = next(tx.PENDING.iterdir())
            journal = tx.load_journal(folder)
            owner = next(owner for owner in journal['owned'] if owner['kind'] == 'report')
            stat = source.stat()
            self.assertEqual((owner['ino'], owner['dev']), (stat.st_ino, stat.st_dev))
            raise Interrupted()
        with patch.object(tx.os, operation, side_effect=abrupt), self.assertRaises(Interrupted):
            self.save()
        self.assertFalse(self.report.exists())
        self.assertFalse(tx.recover_pending())
        self.assert_complete()

    def test_interrupted_rollback_after_post_rename_failure_keeps_ownership(self):
        sync = tx.sync_directory
        def fail_after_publish(folder):
            if folder == self.report.parent and self.report.exists():
                raise OSError('publication interrupted')
            return sync(folder)
        with patch.object(tx, 'sync_directory', side_effect=fail_after_publish), patch.object(tx, 'rollback', side_effect=Interrupted()), self.assertRaises(Interrupted):
            self.save()
        self.assertTrue(self.report.exists())
        journal = tx.load_journal(next(tx.PENDING.iterdir()))
        self.assertEqual(journal['state'], 'aborted')
        self.assertEqual([owner['kind'] for owner in journal['owned']], ['report'])
        self.assertFalse(tx.recover_pending())
        self.assertFalse(self.report.exists())
        self.assertFalse(self.hmi.exists())

import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'local-web'))
import app_settings
import report_storage
import server
import transactions
from fixtures import make_report, TODAY
from report import ReportError


class RecoveryInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.report = self.base / 'final' / '20261027.xlsx'
        self.hmi = self.base / 'hmi' / '송도지소_하수_20261026.xlsx'
        self.report.parent.mkdir(); self.hmi.parent.mkdir()
        for target, name, value in ((transactions, 'PENDING', self.base / 'pending'),
                                    (report_storage, 'RECOVERY_ERRORS', {}),
                                    (server, 'ACTIVE_JOBS', 0), (server, 'SHUTTING_DOWN', False)):
            mock = patch.object(target, name, value)
            mock.start(); self.addCleanup(mock.stop)
        class Quiet(server.Handler):
            def log_message(self, *args): pass
        self.httpd = server.LocalServer(('127.0.0.1', 0), Quiet)
        self.worker = threading.Thread(target=self.httpd.serve_forever)
        self.worker.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.worker.join()

    def api(self, path, body=None):
        request = urllib.request.Request(f'http://127.0.0.1:{self.httpd.server_port}' + path,
            data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def pending(self):
        class Interrupted(BaseException): pass
        publish = transactions.publish_output
        def interrupt(folder, journal, item):
            result = publish(folder, journal, item)
            if item['kind'] == 'report': raise Interrupted()
            return result
        with patch.object(transactions, 'publish_output', side_effect=interrupt), self.assertRaises(Interrupted):
            transactions.commit_pair(make_report(TODAY), self.report,
                make_report(TODAY - dt.timedelta(days=1)), self.hmi, TODAY.isoformat())

    def test_status_is_read_only_and_retry_preserves_conflicts_then_completes(self):
        self.pending()
        stamp = self.report.stat().st_mtime_ns
        journal = next(transactions.PENDING.iterdir()) / 'journal.json'
        original = journal.read_bytes()
        status = self.api('/api/recovery')
        self.assertEqual(status['items'][0]['record_date'], TODAY.isoformat())
        self.assertFalse(self.hmi.exists())
        self.assertEqual(journal.read_bytes(), original)
        self.hmi.write_bytes(b'keep this foreign file')
        blocked = self.api('/api/recovery/retry', {})
        self.assertIn('덮어쓰지 않습니다', blocked['items'][0]['error'])
        self.assertEqual(self.api('/api/recovery')['items'], blocked['items'])
        self.assertEqual(self.hmi.read_bytes(), b'keep this foreign file')
        self.hmi.unlink()
        self.assertEqual(self.api('/api/recovery/retry', {})['items'], [])
        self.assertTrue(self.hmi.is_file())
        self.assertEqual(self.report.stat().st_mtime_ns, stamp)

    def test_malformed_record_remains_visible_and_is_not_removed(self):
        folder = transactions.PENDING / ('a' * 32)
        folder.mkdir(parents=True)
        (folder / 'journal.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(len(self.api('/api/recovery')['items']), 1)
        self.assertEqual(len(self.api('/api/recovery/retry', {})['items']), 1)
        self.assertEqual((folder / 'journal.json').read_text(), '{broken')

    def test_retry_protects_shutdown_and_rejects_overlapping_retry(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def waiting():
            entered.set()
            if not release.wait(4): raise RuntimeError('test wait expired')
            return []
        def request_retry():
            try: self.api('/api/recovery/retry', {})
            except Exception as error: errors.append(error)
        with patch.object(server, 'recover_pending', side_effect=waiting):
            worker = threading.Thread(target=request_retry); worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.api('/api/status')['busy'])
                for path in ('/api/shutdown', '/api/recovery/retry'):
                    with self.assertRaises(urllib.error.HTTPError) as failure: self.api(path, {})
                    self.assertEqual(failure.exception.code, 409)
            finally:
                release.set(); worker.join(5)
        self.assertFalse(errors)
        self.assertFalse(self.api('/api/status')['busy'])

    def test_open_folder_uses_only_matching_configured_output_folder(self):
        config = {'raw_folder': self.base, 'final_folder': self.report.parent, 'hmi_folder': self.hmi.parent}
        opened = Mock()
        with patch.object(app_settings, 'folder_config', return_value=config), patch.object(app_settings, 'os', SimpleNamespace(name='nt', startfile=opened)):
            for kind, folder in (('report', self.report.parent), ('hmi', self.hmi.parent)):
                self.api('/api/open-output-folder', {'kind': kind, 'folder': str(folder)})
                opened.assert_called_with(str(folder), 'open')
            opened.reset_mock()
            for body in ({'kind': 'raw', 'folder': str(self.base)},
                         {'kind': 'report', 'folder': str(self.hmi.parent)}, {'kind': [], 'folder': str(self.base)}):
                with self.assertRaises(urllib.error.HTTPError) as failure: self.api('/api/open-output-folder', body)
                self.assertEqual(failure.exception.code, 400)
            opened.assert_not_called()
            opened.side_effect = OSError('offline')
            with self.assertRaisesRegex(ReportError, '폴더를 열지 못했습니다'):
                app_settings.open_output_folder({'kind': 'report', 'folder': str(self.report.parent)})

    def test_duty_date_listing_and_server_generation_guard(self):
        raw = self.base / 'raw'; raw.mkdir()
        (raw / '20261027.xlsx').write_bytes(make_report(TODAY))
        (self.report.parent / '20261026.xlsx').write_bytes(make_report(TODAY - dt.timedelta(days=1), True))
        config = {'raw_folder': raw, 'final_folder': self.report.parent, 'hmi_folder': self.hmi.parent}
        with patch.object(app_settings, 'folder_config', return_value=config):
            default = self.api('/api/files')
            self.assertEqual(default['duty']['record_date'], (dt.date.today() - dt.timedelta(days=1)).isoformat())
            missing = self.api('/api/files?date=2026-10-28')
            self.assertIsNone(missing['automatic']['raw_id'])
            self.assertIn('20261029.xlsx', missing['automatic']['message'])
            correct = self.api('/api/files?date=' + TODAY.isoformat())
            self.assertEqual(correct['automatic']['raw_id'], 'raw/20261027.xlsx')
            for endpoint in ('/api/inspect', '/api/preview', '/api/generate'):
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    self.api(endpoint, {'record_date': '2026-10-28', 'raw': {'id': 'raw/20261027.xlsx'},
                        'previous': {'id': 'final/20261026.xlsx'}})
                self.assertEqual(failure.exception.code, 400)
                self.assertIn('당직일', json.loads(failure.exception.read())['error'])
                failure.exception.close()
            self.assertFalse(self.hmi.exists())

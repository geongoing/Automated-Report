from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'local-web'))
import app_settings
import drafts
import report_files
import report_storage as storage
from report import generate_with_values
from hmi import render_hmi
from fixtures import TODAY, INPUTS, make_report


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.raw, self.final, self.hmi = [self.base / name for name in ('raw', 'final', 'hmi')]
        for path in (self.raw, self.final, self.hmi): path.mkdir()
        config = patch.object(app_settings, 'CONFIG', self.base / 'settings.json')
        draft = patch.object(drafts, 'DRAFTS', self.base / 'drafts.json')
        config.start(); draft.start()
        self.addCleanup(config.stop); self.addCleanup(draft.stop)
        app_settings.save_settings(str(self.raw), str(self.final), str(self.hmi))
        self.raw_data = make_report(TODAY)
        self.previous = make_report(TODAY - dt.timedelta(days=1), True)
        (self.raw / '20261027.xlsx').write_bytes(self.raw_data)
        (self.final / '20261026.xlsx').write_bytes(self.previous)

    def outputs(self):
        report, summary, values = generate_with_values(self.raw_data, self.previous, 'raw.xlsx', 'previous.xlsx', INPUTS)
        hmi, hmi_summary = render_hmi(values, (ROOT / '송도_HMI.xlsx').read_bytes(), user_input={
            'record_date': TODAY.isoformat(), 'day_workers': [], 'night_workers': ['test1', 'test2'], 'fields': {}})
        return report, summary['output_name'], hmi, hmi_summary['output_name']

    def test_settings_discovery_and_drafts_keep_existing_date_and_empty_slots(self):
        catalog = report_files.catalog()
        pair = report_files.auto_pair(catalog)
        self.assertEqual(pair['raw_id'], 'raw/20261027.xlsx')
        self.assertEqual(pair['previous_id'], 'final/20261026.xlsx')
        saved = {'record_date': TODAY.isoformat(), 'updated_at': 100,
                 'values': {**INPUTS, 'G32': '0', 'B32': ''}, 'day_workers': ['', 'person', '', '', '', '', '', ''],
                 'night_workers': ['', ''], 'hmi_fields': {}, 'hmi_folder': str(self.hmi)}
        drafts.save_draft(saved)
        drafts.save_draft({**saved, 'updated_at': 99, 'values': INPUTS})
        self.assertEqual(drafts.get_draft(TODAY.isoformat())['draft'], saved)
        self.assertEqual((self.final / '20261026.xlsx').read_bytes(), self.previous)

    def test_pair_save_failure_removes_only_its_first_new_file(self):
        args = self.outputs()
        real_save = storage.save_unique
        def save(data, name, *, folder):
            if folder == self.hmi: raise OSError('simulated full disk')
            return real_save(data, name, folder=folder)
        with patch.object(storage, 'save_unique', side_effect=save), self.assertRaises(OSError):
            storage.save_report_pair(*args, record_date=TODAY.isoformat())
        self.assertEqual(list(self.final.iterdir()), [self.final / '20261026.xlsx'])
        self.assertEqual((self.final / '20261026.xlsx').read_bytes(), self.previous)
        self.assertFalse(list(self.hmi.iterdir()))

    def test_concurrent_generation_and_rewrite_allow_only_one_pair_each(self):
        args = self.outputs()
        def save(token=None):
            try:
                storage.save_report_pair(*args, record_date=TODAY.isoformat(), rewrite_token=token)
                return 'created'
            except storage.DuplicateReportError:
                return 'duplicate'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(save, [None, None])), ['created', 'duplicate'])
        token = storage.existing_outputs(TODAY.isoformat())['token']
        protected = {p: p.read_bytes() for p in self.final.iterdir()}
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(save, [token, token])), ['created', 'duplicate'])
        for path, content in protected.items(): self.assertEqual(path.read_bytes(), content)
        self.assertEqual(len(list(self.hmi.glob('*.xlsx'))), 2)

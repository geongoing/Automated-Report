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
import transactions
from report import Book, generate_with_values, patch_cell
from hmi import HMI_SHEET, render_hmi
from fixtures import TODAY, INPUTS, make_report, replace_parts


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.raw, self.final, self.hmi = [self.base / name for name in ('raw', 'final', 'hmi')]
        for path in (self.raw, self.final, self.hmi): path.mkdir()
        config = patch.object(app_settings, 'CONFIG', self.base / 'settings.json')
        draft = patch.object(drafts, 'DRAFTS', self.base / 'drafts.json')
        pending = patch.object(transactions, 'PENDING', self.base / 'pending-writes')
        config.start(); draft.start(); pending.start()
        self.addCleanup(config.stop); self.addCleanup(draft.stop); self.addCleanup(pending.stop)
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
        publish = transactions.publish_output
        def save(folder, journal, item):
            if item['kind'] == 'hmi': raise OSError('simulated full disk')
            return publish(folder, journal, item)
        with patch.object(transactions, 'publish_output', side_effect=save), self.assertRaises(OSError):
            storage.save_report_pair(*args, record_date=TODAY.isoformat())
        self.assertEqual(list(self.final.iterdir()), [self.final / '20261026.xlsx'])
        self.assertEqual((self.final / '20261026.xlsx').read_bytes(), self.previous)
        self.assertFalse(list(self.hmi.iterdir()))

    def test_discovery_skips_corrupt_cells_and_uses_internal_dates_at_boundaries(self):
        template = (ROOT / '송도_HMI.xlsx').read_bytes()
        hmi_book = Book(template, 'template.xlsx', require_report=False)
        hmi_path = hmi_book.sheets[HMI_SHEET]
        hmi_xml = hmi_book.zip.read(hmi_path).decode()
        for index, date in enumerate((dt.date(2027, 1, 1), dt.date(2028, 3, 1))):
            with self.subTest(date=date):
                raw = self.raw / f'renamed-raw-{index}.xlsx'
                previous = self.final / f'renamed-final-{index}.xlsx'
                raw.write_bytes(make_report(date))
                previous.write_bytes(make_report(date - dt.timedelta(days=1), True))
                damaged = make_report(date, overrides={'B32': 'bad-number'})
                (self.raw / f'damaged-{index}.xlsx').write_bytes(damaged)
                files = report_files.catalog()
                pair = report_files.auto_pair(files)
                self.assertEqual(pair['raw_id'], 'raw/' + raw.name)
                self.assertEqual(pair['previous_id'], 'final/' + previous.name)
                self.assertNotIn(f'raw/damaged-{index}.xlsx', [file['id'] for file in files])
                yesterday = date - dt.timedelta(days=1)
                previous_hmi = self.hmi / f'renamed-hmi-{index}.xlsx'
                xml = patch_cell(hmi_xml, 'B8', yesterday.strftime('근무 일지(%Y.%m.%d)'))
                previous_hmi.write_bytes(replace_parts(template, {hmi_path: xml}))
                # A newer mtime cannot make an older record date eligible.
                wrong = patch_cell(hmi_xml, 'B8', (date - dt.timedelta(days=2)).strftime('근무 일지(%Y.%m.%d)'))
                (self.hmi / 'wrong-date-newer-file.xlsx').write_bytes(replace_parts(template, {hmi_path: wrong}))
                selected, _ = report_files.previous_hmi(date)
                self.assertTrue(selected.samefile(previous_hmi))

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

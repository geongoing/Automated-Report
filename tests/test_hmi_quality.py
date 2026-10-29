import datetime as dt
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'local-web'))
from report import Book, ReportError, generate_with_values, patch_cell
from hmi import HMI_SHEET, compose_h52, render_hmi, validate_water_quality, hmi_record_date
from fixtures import TODAY, INPUTS, WATER_QUALITY, make_report, replace_parts


class HmiQualityTests(unittest.TestCase):
    def test_measurement_replacement_preserves_notes_spacing_and_units(self):
        original = '○ 소내 순찰 및 점검\n○ 유입수 및 방류수 시료 채수\n  - 유입수 pH: 6.82   수온: 26.6℃ \n  - 방류수 pH: 6.78    수온: 27.2℃\n○ 기타 점검 완료'
        expected = original.replace('pH: 6.82', 'pH: 6.8').replace('수온: 26.6', '수온: 27.2').replace('pH: 6.78', 'pH: 7.20').replace('수온: 27.2℃\n○', '수온: 26.8℃\n○')
        self.assertEqual(compose_h52(original, WATER_QUALITY), expected)
        self.assertIn('유입수 pH: 6.8', compose_h52('', WATER_QUALITY))

    def test_all_four_measurements_are_required_and_invalid_ph_is_rejected(self):
        for data in (None, {}, {**WATER_QUALITY, 'inlet_ph': ''}, {**WATER_QUALITY, 'inlet_ph': '14.1'},
                     {**WATER_QUALITY, 'outlet_temperature': 'NaN'}, {**WATER_QUALITY, 'outlet_temperature': '1e99999999'}):
            with self.subTest(data=data), self.assertRaises(ReportError): validate_water_quality(data)
        self.assertEqual(validate_water_quality({key: '0' for key in WATER_QUALITY}), {key: '0' for key in WATER_QUALITY})

    def test_render_writes_h52_and_legacy_style_name_with_full_internal_date(self):
        template = (ROOT / '송도_HMI.xlsx').read_bytes()
        book = Book(template, 'template.xlsx', require_report=False)
        sheet = book.sheets[HMI_SHEET]
        xml = patch_cell(book.zip.read(sheet).decode(), 'B8', (TODAY - dt.timedelta(days=1)).strftime('근무 일지(%m.%d)'))
        xml = patch_cell(xml, 'H52', '○ 유지할 문구\n  - 유입수 pH: 1   수온: 2℃\n  - 방류수 pH: 3   수온: 4℃')
        previous = replace_parts(template, {sheet: xml})
        _, _, values = generate_with_values(make_report(TODAY), make_report(TODAY - dt.timedelta(days=1), True), 'raw.xlsx', 'previous.xlsx', INPUTS)
        data, summary = render_hmi(values, template, previous_data=previous, user_input={
            'record_date': TODAY.isoformat(), 'day_workers': [], 'night_workers': ['test1', 'test2'], 'fields': {}, 'water_quality': WATER_QUALITY})
        output = Book(data, 'output.xlsx', require_report=False)
        self.assertEqual(summary['output_name'], '송도_HMI 중앙제어실 _운영일지(10.26).xlsx')
        self.assertEqual(hmi_record_date(output), TODAY)
        text = output.cell('H52', HMI_SHEET)
        self.assertIn('○ 유지할 문구', text)
        self.assertIn('유입수 pH: 6.8   수온: 27.2℃', text)
        self.assertIn('방류수 pH: 7.20   수온: 26.8℃', text)

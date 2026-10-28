import datetime as dt
import http.client
from decimal import Decimal
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'local-web'))
import server
from report import Book,ReportError,generate_with_values,excel_round,patch_cell
from hmi import HMI_SHEET,render_hmi,CELL_MAP,CELL_DIGITS,DAY_CELLS,NIGHT_CELLS,OTHER_CARRY_CELLS,date_text
from fixtures import make_report,replace_parts,TODAY,INPUTS


class ReportTests(unittest.TestCase):
    def test_all_carry_calculations_and_hmi_cells_have_the_expected_values(self):
        # Independent mapping contract: every HMI destination, including sparse
        # MLSS and differing chemical/runtime source rows, must remain covered.
        contract = '''
13 BCDEFGHIJ 6 ABCDEFGHI
17 CDEFGHIJ 28 BCDEFGHI
18 CDEFGHI 29 BCDEFGH
21 CDEFGHIJ 10 BCDEFGHI
23 CDEFGHIJ 12 BCDEFGHI
25 CFHIJ 12 KLOPQ
29 CDEFGHIJ 10 JKLMNOPQ
30 CDEFGHIJ 16 HIJKLMNO
31 CDEFGHIJ 18 HIJKLMNO
36 C 12 M
40 DE 17 EF
41 DE 18 EF
42 DE 19 EF
43 DE 20 EF
44 DE 21 EF
45 DE 22 EF
40 F 21 H
41 F 21 I
42 F 21 J
43 F 21 K
44 F 21 L
45 F 21 M
40 IJK 41 BCD
41 IJK 42 BCD
42 IJK 43 BCD
48 BCDEFGH 32 ABCDEFG
48 IJK 25 ABC
52 CDEFG 37 BCDEF
'''
        expected_map = {}
        for line in contract.splitlines():
            if not line: continue
            dest_row, dest_columns, source_row, source_columns = line.split()
            expected_map.update({dc + dest_row: sc + source_row for dc, sc in zip(dest_columns, source_columns)})
        self.assertEqual(CELL_MAP, expected_map)
        expected_digits = dict.fromkeys(expected_map, 0)
        expected_digits.update({column + '23': 2 for column in 'CDEFGHIJ'})
        expected_digits.update({column + '48': 2 for column in 'BCDEFGH'})
        expected_digits.update({f'E{row}': 3 for row in range(40, 46)})
        expected_digits.update({'D45': 3, 'C52': 3, 'D52': 3})
        self.assertEqual(CELL_DIGITS, expected_digits)
        source_values = {f'{col}{row}': Decimal(row * 100 + index) + Decimal('0.125')
                         for row in range(1, 61) for index, col in enumerate('ABCDEFGHIJKLMNOPQ', 1)}
        previous_values = {'C32': '26.074999999999999', 'D32': '12.345', 'F32': '1.235',
                           'D37': '10.1235', 'E37': '20.3455', 'F37': '30.5555',
                           'B42': '100.5', 'C42': '200.5', 'D42': '300.5', 'F42': '1000.5'}
        raw = make_report(TODAY, overrides=source_values)
        previous = make_report(TODAY - dt.timedelta(days=1), True, previous_values)
        data, summary, values = generate_with_values(raw, previous, 'raw.xlsx', 'previous.xlsx', INPUTS)
        final = Book(data, 'final.xlsx')
        expected_carry = {'C33': '26.08', 'D33': '12.35', 'F33': '1.24', 'D36': '10.124',
                          'E36': '20.346', 'F36': '30.556', 'B41': '101', 'C41': '201', 'D41': '301', 'F41': '1001'}
        for cell, value in expected_carry.items():
            self.assertEqual(final.numeric(cell), Decimal(value), cell)
        expected_computed = {'A32': Decimal('6370.82'), 'E32': Decimal('3204.885'),
                             'B42': Decimal('4403.125'), 'D42': Decimal('4605.125'),
                             'C43': Decimal('4002.125'), 'F43': Decimal('99')}
        for cell, value in expected_computed.items():
            self.assertEqual(final.numeric(cell), value, cell)
        self.assertEqual(final.numeric('D20'), source_values['D20'])
        hmi_data, result = render_hmi(values, (ROOT / '송도_HMI.xlsx').read_bytes())
        output = Book(hmi_data, 'hmi.xlsx', require_report=False)
        for target, source in expected_map.items():
            expected = excel_round(final.numeric(source), expected_digits[target])
            self.assertEqual(output.numeric(target, HMI_SHEET), expected, f'{target} <- {source}')
        self.assertEqual(result['mapped_count'], len(expected_map) + 1)

    def test_dates_cross_month_year_and_leap_day_without_changing_carry(self):
        for date in (dt.date(2026, 1, 1), dt.date(2026, 12, 31), dt.date(2026, 3, 1),
                     dt.date(2028, 2, 29), dt.date(2028, 3, 1)):
            with self.subTest(date=date):
                raw, previous = make_report(date), make_report(date - dt.timedelta(days=1), True)
                data, summary, values = generate_with_values(raw, previous, 'renamed.xlsx', 'previous.xlsx', INPUTS)
                self.assertEqual(summary['output_name'], (date + dt.timedelta(days=1)).strftime('%Y%m%d.xlsx'))
                self.assertEqual(summary['previous_date'], (date - dt.timedelta(days=1)).isoformat())
                self.assertEqual(Book(data, 'final.xlsx').numeric('A32'), 6)
                _, result = render_hmi(values, (ROOT / '송도_HMI.xlsx').read_bytes())
                self.assertEqual(result['output_name'], date.strftime('송도지소_하수_%Y%m%d.xlsx'))
                with self.assertRaises(ReportError):
                    generate_with_values(raw, make_report(date - dt.timedelta(days=2), True), 'raw.xlsx', 'wrong.xlsx', INPUTS)

    def test_previous_hmi_dates_convert_epochs_and_workers_are_always_replaced(self):
        date = dt.date(2028, 3, 1)
        yesterday = date - dt.timedelta(days=1)
        template = (ROOT / '송도_HMI.xlsx').read_bytes()
        book = Book(template, 'template.xlsx', require_report=False)
        template_1904 = replace_parts(template, {'xl/workbook.xml': book.workbook_xml.decode().replace('<workbookPr ', '<workbookPr date1904="1" ')})
        _, _, previous_values = generate_with_values(make_report(yesterday), make_report(yesterday - dt.timedelta(days=1), True), 'raw.xlsx', 'previous.xlsx', INPUTS)
        previous, _ = render_hmi(previous_values, template_1904, user_input={
            'record_date': yesterday.isoformat(), 'day_workers': ['old-day'], 'night_workers': ['old1', 'old2'],
            'fields': {'C32': '2028-02-29', 'C28': '가동', 'H52': '이월 테스트'}})
        previous_book = Book(previous, 'previous-hmi.xlsx', require_report=False)
        sheet = previous_book.sheets[HMI_SHEET]
        xml = previous_book.zip.read(sheet).decode()
        for index, cell in enumerate(OTHER_CARRY_CELLS):
            xml = patch_cell(xml, cell, Decimal(index + 100))
        previous = replace_parts(previous, {sheet: xml})
        _, _, values = generate_with_values(make_report(date), make_report(yesterday, True), 'raw.xlsx', 'previous.xlsx', INPUTS)
        data, _ = render_hmi(values, template, previous_data=previous, user_input={
            'record_date': date.isoformat(), 'day_workers': [], 'night_workers': ['new1', 'new2'], 'fields': {}})
        output = Book(data, 'hmi.xlsx', require_report=False)
        self.assertEqual(date_text(output.cell('C32', HMI_SHEET), output), '2028-02-29')
        self.assertEqual(output.cell('C28', HMI_SHEET), '가동')
        self.assertEqual(output.cell('H52', HMI_SHEET), '이월 테스트')
        for index, cell in enumerate(OTHER_CARRY_CELLS):
            self.assertEqual(output.numeric(cell, HMI_SHEET), Decimal(index + 100), cell)
        for cell in DAY_CELLS:
            self.assertIsNone(output.cell(cell, HMI_SHEET), cell)
        self.assertEqual([output.cell(cell, HMI_SHEET) for cell in NIGHT_CELLS], ['new1', 'new2'])
        with self.assertRaisesRegex(ReportError, '하루 전'):
            render_hmi(values, template, previous_data=data, user_input={
                'record_date': date.isoformat(), 'day_workers': [], 'night_workers': ['new1', 'new2'], 'fields': {}})

    def test_malformed_cells_raise_a_readable_report_error(self):
        raw = make_report(TODAY)
        book = Book(raw, 'raw.xlsx')
        path = book.sheets['운영일지']
        original = book.zip.read(path).decode()
        for broken in ('<c r="B32"><v>not-a-number</v></c>', '<c r="B32" t="s"><v>999</v></c>'):
            damaged = replace_parts(raw, {path: original.replace('<c r="B32"><v>1</v></c>', broken)})
            with self.subTest(cell=broken), self.assertRaisesRegex(ReportError, '운영일지!B32'):
                Book(damaged, 'damaged.xlsx').cell('B32')

    def test_required_zero_cake_formula_original_d20_and_hmi(self):
        raw=make_report(TODAY);previous=make_report(TODAY-dt.timedelta(days=1),True)
        with self.assertRaises(ReportError):generate_with_values(raw,previous,'raw.xlsx','previous.xlsx',{**INPUTS,'G32':''})
        data,summary,values=generate_with_values(raw,previous,'raw.xlsx','previous.xlsx',INPUTS)
        final=Book(data,'output.xlsx')
        self.assertEqual(final.numeric('G32'),0);self.assertEqual(final.numeric('A32'),6)
        self.assertEqual(final.numeric('D20'),77);self.assertEqual(final.numeric('F43'),100)
        self.assertIn(b'<f>B32+C32+D32-C33-D33</f>',final.zip.read(final.sheets['운영일지']))
        hmi,result=render_hmi(values,(ROOT/'송도_HMI.xlsx').read_bytes(),user_input={'record_date':TODAY.isoformat(),'day_workers':[],'night_workers':['test1','test2'],'fields':{}})
        hmi=Book(hmi,'hmi.xlsx',require_report=False)
        self.assertEqual(hmi.numeric('H48',HMI_SHEET),0)
        self.assertEqual(hmi.numeric('B48',HMI_SHEET),6)

    def test_shutdown_refuses_active_generation(self):
        class Quiet(server.Handler):
            def log_message(self,*args):pass
        httpd=server.LocalServer(('127.0.0.1',0),Quiet)
        worker=threading.Thread(target=httpd.serve_forever,daemon=True);worker.start()
        try:
            with patch.object(server,'ACTIVE_JOBS',1),patch.object(server,'SHUTTING_DOWN',False):
                req=urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}/api/shutdown',data=b'{}',headers={'Content-Type':'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as failure:urllib.request.urlopen(req,timeout=3)
                self.assertEqual(failure.exception.code,409);self.assertFalse(server.SHUTTING_DOWN)
        finally:httpd.shutdown();httpd.server_close();worker.join(timeout=3)

    def test_malformed_origin_is_rejected_without_aborting_the_connection(self):
        class Quiet(server.Handler):
            def log_message(self, *args): pass
        httpd = server.LocalServer(('127.0.0.1', 0), Quiet)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            port = httpd.server_port
            for origin in ('http://127.0.0.1:invalid', 'http://[broken', 'http://127.0.0.1:99999',
                           f'https://127.0.0.1:{port}', f'http://user@localhost:{port}',
                           f'http://localhost:{port}/unexpected', 'null'):
                for method, path in (('GET', '/api/status'), ('POST', '/api/generate')):
                    with self.subTest(origin=origin, method=method):
                        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                        try:
                            conn.request(method, path, body='{}' if method == 'POST' else None,
                                         headers={'Origin': origin, 'Content-Type': 'application/json'})
                            response = conn.getresponse()
                            self.assertEqual(response.status, 403)
                            response.read()
                        finally: conn.close()
            for origin in (None, f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                try:
                    conn.request('GET', '/api/status', headers={'Origin': origin} if origin else {})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
                finally: conn.close()
        finally:
            httpd.shutdown(); httpd.server_close(); worker.join(timeout=3)


if __name__=='__main__':unittest.main()

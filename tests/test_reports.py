import datetime as dt
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
from report import Book,ReportError,generate_with_values
from hmi import HMI_SHEET,render_hmi
from fixtures import make_report,TODAY,INPUTS


class ReportTests(unittest.TestCase):
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


if __name__=='__main__':unittest.main()

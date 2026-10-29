"""Exercise the standalone Windows EXE without Python or operational fixtures."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from fixtures import INPUTS,WATER_QUALITY,TODAY,make_report,pe_bytes,replace_parts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'local-web'))
from report import Book, patch_cell
from hmi import HMI_SHEET


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--compat-exe',type=Path)
    args=parser.parse_args()
    original=args.exe.resolve()
    with tempfile.TemporaryDirectory(prefix='songdo-frozen-') as temporary:
        base=Path(temporary).resolve()
        program=base/'program';program.mkdir();exe=program/'launch.exe';shutil.copy2(original,exe)
        environment=dict(os.environ)
        environment['LOCALAPPDATA']=str(base/'user-profile')
        environment['PATH']=str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32')
        for key in ('PYTHONHOME','PYTHONPATH','VIRTUAL_ENV'):environment.pop(key,None)
        profile=Path(environment['LOCALAPPDATA'])/'SongdoDaily'
        marker=profile/'server.json'
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        running=False
        def run(*extra):
            started=time.perf_counter()
            result=subprocess.run([str(exe),*extra],env=environment,cwd=program,timeout=100,
                                  stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
            assert result.returncode==0,(extra,result.returncode)
            return time.perf_counter()-started
        def api(path,body=None):
            port=json.loads(marker.read_text(encoding='utf-8'))['port']
            request=urllib.request.Request(f'http://127.0.0.1:{port}'+path,
                data=None if body is None else json.dumps(body).encode(),headers={} if body is None else {'Content-Type':'application/json'})
            with urllib.request.urlopen(request,timeout=5) as response:
                raw=response.read()
                return json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw
        def stop():
            run('--stop')
            for _ in range(50):
                if not marker.exists():break
                time.sleep(.1)
            assert not marker.exists()
            time.sleep(.7)
        try:
            cold=run('--no-browser','--skip-update');running=True
            status=api('/api/status');current=status['version']
            assert status['execution_mode']=='exe' and Path(status['executable']).is_relative_to(profile/'app/versions')
            assert list(program.iterdir())==[exe]
            settings=api('/api/settings')
            assert settings['configured'] is False and settings['hmi_available']
            assert all(settings[key]=='' for key in ('raw_folder','final_folder','hmi_folder'))
            pid=json.loads(marker.read_text(encoding='utf-8'))['pid']
            warm=run('--no-browser','--skip-update')
            assert json.loads(marker.read_text(encoding='utf-8'))['pid']==pid
            print(f'STARTUP: cold={cold:.3f}s, existing server={warm:.3f}s (offline check)',flush=True)
            print('PASS: standalone managed EXE; no Python; no files beside launch EXE; first setup blank',flush=True)
            if args.compat_exe:
                legacy=base/'legacy.exe';shutil.copy2(args.compat_exe.resolve(),legacy)
                result=subprocess.run([str(legacy),'--no-browser','--skip-update'],env=environment,cwd=base,
                                      timeout=100,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
                assert result.returncode==0 and api('/api/status')['version']==current
                assert json.loads(marker.read_text(encoding='utf-8'))['pid']==pid
                print('PASS: older entry EXE delegates to current installed build without restarting server',flush=True)
            raw,final,hmi=[base/name for name in ('raw','final','hmi')]
            for folder in (raw,final,hmi):folder.mkdir()
            (raw/'20261027.xlsx').write_bytes(make_report(TODAY))
            (final/'20261026.xlsx').write_bytes(make_report(TODAY-dt.timedelta(days=1),True))
            api('/api/settings',{'raw_folder':str(raw),'final_folder':str(final),'hmi_folder':str(hmi)})
            pair=api('/api/files?date='+TODAY.isoformat())['automatic']
            missing=api('/api/files?date=2026-10-28')['automatic']
            assert missing['raw_id'] is None and '20261029.xlsx' in missing['message']
            body={'record_date':TODAY.isoformat(),'raw':{'id':pair['raw_id']},'previous':{'id':pair['previous_id']},'inputs':INPUTS}
            preview=api('/api/preview',body);body['source_token']=preview['source_token']
            template=(Path(__file__).resolve().parents[1]/'송도_HMI.xlsx').read_bytes()
            template_book=Book(template,'template.xlsx',require_report=False)
            sheet=template_book.sheets[HMI_SHEET]
            legacy_xml=patch_cell(template_book.zip.read(sheet).decode(),'B8',(TODAY-dt.timedelta(days=1)).strftime('근무 일지(%m.%d)'))
            legacy_xml=patch_cell(legacy_xml,'C28','정지')
            legacy_path=hmi/'legacy-previous.xlsx'
            legacy_data=replace_parts(template,{sheet:legacy_xml})
            legacy_path.write_bytes(legacy_data)
            previous_hmi=api('/api/hmi/profile',{'record_date':TODAY.isoformat()})
            assert previous_hmi['previous_name']==legacy_path.name and previous_hmi['fields']['C28']=='정지'
            body['hmi']={**previous_hmi,'day_workers':[],'night_workers':['test1','test2'],'water_quality':WATER_QUALITY}
            generated=api('/api/generate',body)
            assert generated['outputs'][1]['name']==f'송도_HMI 중앙제어실 _운영일지({TODAY:%m.%d}).xlsx'
            measured=Book(Path(generated['outputs'][1]['path']).read_bytes(),'hmi.xlsx',require_report=False).cell('H52',HMI_SHEET)
            assert 'pH: 6.8' in measured and 'pH: 7.20' in measured and '수온: 27.2' in measured and '수온: 26.8' in measured
            assert Book(Path(generated['outputs'][1]['path']).read_bytes(),'hmi.xlsx',require_report=False).cell('C28',HMI_SHEET)=='정지'
            assert legacy_path.read_bytes()==legacy_data
            assert len(generated['outputs'])==2
            assert all(Path(item['folder']).samefile(Path(item['path']).parent) for item in generated['outputs'])
            assert api('/api/recovery')['items']==[]
            original_hashes={item['path']:hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() for item in generated['outputs']}
            try:api('/api/generate',body)
            except urllib.error.HTTPError as error:assert error.code==409
            else:raise AssertionError('duplicate accepted')
            result=api('/api/generate',{**body,'rewrite_token':generated['existing']['token']})
            assert all(Path(item['path']).stem.endswith('_2') for item in result['outputs'])
            for path,digest in original_hashes.items():assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
            draft={'record_date':TODAY.isoformat(),'updated_at':int(time.time()*1000),'values':INPUTS,
                   'day_workers':['','','','','','','',''],'night_workers':['test1','test2'],'hmi_fields':{},'water_quality':WATER_QUALITY,'hmi_folder':str(hmi)}
            api('/api/draft',draft)
            protected={path:path.read_bytes() for path in (profile/'settings.json',profile/'drafts.json')}
            assert list((profile/'backups/settings').glob('good-*.json'))
            assert list((profile/'backups/drafts').glob('good-*.json'))
            stop();running=False
            for path in protected:path.write_bytes(b'{damaged state')
            run('--no-browser','--skip-update');running=True
            assert api('/api/draft?date='+TODAY.isoformat())['draft']==draft
            for path,data in protected.items():
                assert path.read_bytes()==data
                assert (profile/'backups'/path.stem/'damaged.json').read_bytes()==b'{damaged state'
            print('PASS: synthetic report/HMI generation, duplicates/rewrite, original preservation, corrupted settings/drafts restored on EXE restart',flush=True)
            stop();running=False
            # Build an interrupted transaction in the isolated profile, then let
            # the bundled EXE (not this source process) resume it on startup.
            crash_script='''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import transactions as tx
tx.PENDING=Path(sys.argv[2])/'pending-writes'
publish=tx.publish_output
def abrupt(folder,journal,item):
    result=publish(folder,journal,item)
    if item['kind']=='report':os._exit(73)
    return result
tx.publish_output=abrupt
tx.commit_pair(Path(sys.argv[3]).read_bytes(),Path(sys.argv[4]),Path(sys.argv[5]).read_bytes(),Path(sys.argv[6]),sys.argv[7])
'''
            recovered_report=final/f'{TODAY+dt.timedelta(days=1):%Y%m%d}_3.xlsx'
            recovered_hmi=hmi/f'송도_HMI 중앙제어실 _운영일지({TODAY:%m.%d})_3.xlsx'
            crash=subprocess.run([sys.executable,'-B','-c',crash_script,str(Path(__file__).resolve().parents[1]/'local-web'),str(profile),
                                  generated['outputs'][0]['path'],str(recovered_report),generated['outputs'][1]['path'],str(recovered_hmi),TODAY.isoformat()],
                                  env=environment,timeout=30,creationflags=flags)
            assert crash.returncode==73 and recovered_report.exists() and not recovered_hmi.exists()
            stamp=recovered_report.stat().st_mtime_ns
            recovered_hmi.write_bytes(b'foreign file must be preserved')
            run('--no-browser','--skip-update');running=True
            for _ in range(100):
                if not api('/api/status')['busy']:break
                time.sleep(.1)
            blocked=api('/api/recovery')
            assert len(blocked['items'])==1 and blocked['items'][0]['record_date']==TODAY.isoformat()
            assert '덮어쓰지 않습니다' in blocked['items'][0]['error']
            assert len(api('/api/recovery/retry',{})['items'])==1
            assert recovered_hmi.read_bytes()==b'foreign file must be preserved'
            recovered_hmi.unlink()  # Remove only this test's synthetic conflicting file.
            assert api('/api/recovery/retry',{})['items']==[]
            assert api('/api/recovery')['items']==[]
            assert recovered_hmi.read_bytes()==Path(generated['outputs'][1]['path']).read_bytes()
            assert recovered_report.stat().st_mtime_ns==stamp
            assert not list((profile/'pending-writes').iterdir())
            for path,digest in original_hashes.items():assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
            print('PASS: recovery status shows conflict; retry preserves foreign file then recovers missing HMI without rewriting originals',flush=True)
            stop();running=False
            # A hash-correct file with basic PE fields but an invalid loader image
            # exercises real Windows launch failure and rollback without publishing it.
            state=json.loads((profile/'app-state.json').read_text(encoding='utf-8'))
            good=state['active'];bad_data=pe_bytes();bad_hash=hashlib.sha256(bad_data).hexdigest()
            bad=profile/'app/versions'/('999.0.0-'+bad_hash[:16])/'SongdoDaily.exe'
            bad.parent.mkdir(parents=True);bad.write_bytes(bad_data)
            state['previous']=good
            state['active']={'version':'999.0.0','sha256':bad_hash,'path':str(bad)}
            (profile/'app-state.json').write_text(json.dumps(state),encoding='utf-8')
            run('--no-browser','--skip-update');running=True
            assert api('/api/status')['version']==current
            recovered=json.loads((profile/'app-state.json').read_text(encoding='utf-8'))
            assert recovered['active']==good and recovered['failed']['version']=='999.0.0'
            for path,data in protected.items():assert path.read_bytes()==data
            assert list(program.iterdir())==[exe]
            print('PASS: real failed Windows launch restored bundled version and kept settings/drafts unchanged',flush=True)
        finally:
            if running:stop()
            time.sleep(1)
    print('ALL FROZEN CHECKS PASSED',flush=True)


if __name__=='__main__':main()

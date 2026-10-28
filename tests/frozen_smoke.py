"""Exercise the standalone Windows EXE without Python or operational fixtures."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from fixtures import INPUTS,TODAY,make_report,pe_bytes


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
            pair=api('/api/files')['automatic']
            body={'raw':{'id':pair['raw_id']},'previous':{'id':pair['previous_id']},'inputs':INPUTS}
            preview=api('/api/preview',body);body['source_token']=preview['source_token']
            previous_hmi=api('/api/hmi/profile',{'record_date':TODAY.isoformat()})
            body['hmi']={**previous_hmi,'day_workers':[],'night_workers':['test1','test2']}
            generated=api('/api/generate',body)
            assert len(generated['outputs'])==2
            original_hashes={item['path']:hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() for item in generated['outputs']}
            try:api('/api/generate',body)
            except urllib.error.HTTPError as error:assert error.code==409
            else:raise AssertionError('duplicate accepted')
            result=api('/api/generate',{**body,'rewrite_token':generated['existing']['token']})
            assert all(Path(item['path']).stem.endswith('_2') for item in result['outputs'])
            for path,digest in original_hashes.items():assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
            draft={'record_date':TODAY.isoformat(),'updated_at':int(time.time()*1000),'values':INPUTS,
                   'day_workers':['','','','','','','',''],'night_workers':['test1','test2'],'hmi_fields':{},'hmi_folder':str(hmi)}
            api('/api/draft',draft)
            protected={path:path.read_bytes() for path in (profile/'settings.json',profile/'drafts.json')}
            stop();running=False
            run('--no-browser','--skip-update');running=True
            assert api('/api/draft?date='+TODAY.isoformat())['draft']==draft
            print('PASS: synthetic report/HMI generation, duplicates and explicit rewrite, original preservation and autosave restart',flush=True)
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

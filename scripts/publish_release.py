"""Publish verified files as one stable release; keep failed uploads as drafts."""
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'local-web'))
from updater import ASSET,REPOSITORY,validate_manifest


def main():
    if os.environ.get('GITHUB_REPOSITORY')!=REPOSITORY:
        raise SystemExit('Release repository does not match the updater source')
    token=os.environ['GH_TOKEN']
    commit=os.environ['GITHUB_SHA']
    folder=ROOT/'dist'
    manifest=validate_manifest(json.loads((folder/'update.json').read_text(encoding='utf-8')))
    current=json.loads((ROOT/'local-web/version.json').read_text(encoding='utf-8'))['version']
    if manifest['version']!=current:raise SystemExit('Manifest/version mismatch')
    tag='v'+current
    files=[folder/ASSET,folder/'update.json',folder/'SHA256SUMS.txt',folder/f'SongdoDaily-{tag}.zip']
    if hashlib.sha256(files[0].read_bytes()).hexdigest()!=manifest['sha256'] or files[0].stat().st_size!=manifest['size']:
        raise SystemExit('EXE/manifest mismatch')
    def api(path,method='GET',data=None,content_type='application/json',upload=False):
        url=('https://uploads.github.com' if upload else 'https://api.github.com')+path
        raw=json.dumps(data,ensure_ascii=False).encode() if isinstance(data,dict) else data
        request=urllib.request.Request(url,method=method,data=raw,headers={'Authorization':'Bearer '+token,
            'Accept':'application/vnd.github+json','Content-Type':content_type,'User-Agent':'SongdoDaily-Release',
            'X-GitHub-Api-Version':'2026-03-10'})
        with urllib.request.urlopen(request,timeout=60) as response:return json.load(response)
    prefix='/repos/'+REPOSITORY+'/releases'
    try:
        release=api(prefix+'/tags/'+tag)
    except urllib.error.HTTPError as error:
        if error.code!=404:raise
        release=api(prefix,'POST',{'tag_name':tag,'target_commitish':commit,'name':tag,
            'body':(ROOT/'docs/releases'/f'{tag}.md').read_text(encoding='utf-8'),'draft':True,'prerelease':False})
    else:
        if not release['draft']:
            raise SystemExit('Published releases must not be replaced; increment the version')
    existing={item['name']:item for item in release.get('assets',[])}
    for path in files:
        expected='sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()
        if path.name in existing:
            asset=existing[path.name]
        else:
            content_type='application/zip' if path.suffix=='.zip' else 'application/json' if path.suffix=='.json' else 'application/octet-stream'
            asset=api(prefix+'/'+str(release['id'])+'/assets?'+urllib.parse.urlencode({'name':path.name}),
                      'POST',path.read_bytes(),content_type,upload=True)
        if asset.get('digest')!=expected or asset.get('size')!=path.stat().st_size:
            raise SystemExit('Uploaded asset digest does not match: '+path.name)
        print('Verified asset:',path.name)
    published=api(prefix+'/'+str(release['id']),'PATCH',{'draft':False,'prerelease':False,'make_latest':'true'})
    if published['draft']:raise SystemExit('Release was not published')
    print('Published:',published['html_url'])


if __name__=='__main__':main()

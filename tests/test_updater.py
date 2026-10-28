import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'local-web'))
import updater as u
from fixtures import pe_bytes


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.paths=patch.multiple(u,DATA=self.root,VERSIONS=self.root/'app/versions',STATE=self.root/'app-state.json',UPDATE_STATUS=self.root/'update-status.json',SERVER_INFO=self.root/'server.json')
        self.paths.start()
        self.args=SimpleNamespace(managed=True,skip_update=False,port=None)
        self.addCleanup(self.paths.stop)
        self.addCleanup(self.temp.cleanup)

    def item(self,value='2.0.0',data=None):
        data=pe_bytes() if data is None else data
        sha=hashlib.sha256(data).hexdigest()
        item=u.record(value,sha)
        path=Path(item['path']);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        return item

    def manifest(self,value='2.1.0',data=None):
        data=pe_bytes() if data is None else data
        return dict(schema=1,channel='stable',platform='windows-x64',version=value,asset=u.ASSET,size=len(data),sha256=hashlib.sha256(data).hexdigest())

    def test_stable_versions_only(self):
        self.assertGreater(u.version('2.10.0'),u.version('2.9.9'))
        for value in ['2.0.0-beta','v2.0.0','02.0.0','2.0','../2.0.0',None]:
            with self.subTest(value=value),self.assertRaises(u.UpdateError):u.version(value)

    def test_manifest_rejects_untrusted_fields(self):
        for change in [dict(channel='beta'),dict(platform='linux'),dict(asset='../run.exe'),dict(size=True),dict(size=u.MAX_EXE+1),dict(sha256='x'*64),dict(schema=9),dict(version='2.1.0-rc')]:
            with self.subTest(change=change),self.assertRaises(u.UpdateError):u.validate_manifest({**self.manifest(),**change})
        result=u.validate_manifest({**self.manifest(), 'url':'https://untrusted.invalid/program.exe'})
        self.assertNotIn('url',result)

    def test_download_is_pinned_and_verified(self):
        manifest=self.manifest()
        with patch.object(u,'open_download',return_value=io.BytesIO(pe_bytes())) as download:
            item=u.download_release(manifest)
        self.assertTrue(u.valid_record(item))
        self.assertEqual(download.call_args.args[0],u.RELEASES+'/download/v2.1.0/'+u.ASSET)
        self.assertFalse(list(self.root.rglob('*.tmp')))

    def test_bad_hash_length_or_exe_never_installed(self):
        for data,manifest in [(b'bad',self.manifest()),(pe_bytes()+b'extra',self.manifest()),(b'not a PE',self.manifest(data=b'not a PE'))]:
            with self.subTest(data=data[:10]),patch.object(u,'open_download',return_value=io.BytesIO(data)),self.assertRaises(u.UpdateError):
                u.download_release(manifest)
            self.assertFalse(Path(u.record(manifest['version'],manifest['sha256'])['path']).exists())
            self.assertFalse(list(self.root.rglob('*.tmp')))

    def test_record_cannot_escape_or_hide_corruption(self):
        item=self.item();self.assertTrue(u.valid_record(item))
        self.assertFalse(u.valid_record({**item,'path':str(self.root/'outside.exe')}))
        Path(item['path']).write_bytes(b'changed')
        self.assertFalse(u.valid_record(item));self.assertFalse(u.valid_record(None))

    def test_projected_leaf_file_is_valid_but_foreign_record_is_not(self):
        item=self.item();target=Path(item['path']);original=Path.resolve
        def projected(path,*args,**kwargs):
            if path==target:return self.root/'windows-projected-file.exe'
            return original(path,*args,**kwargs)
        with patch.object(Path,'resolve',projected):
            self.assertTrue(u.valid_record(item))
            self.assertFalse(u.valid_record({**item,'path':str(self.root/'foreign.exe')}))

    def test_current_build_does_not_spawn_an_identical_controller(self):
        own=self.item()
        with patch.object(u,'bundled_record',return_value=own),patch.object(u,'managed_launch',return_value='http://ready') as manage,patch.object(u,'run_record') as run:
            self.assertEqual(u.launch('2.0.0',self.args,lambda args:None),'http://ready')
            manage.assert_called_once();run.assert_not_called()

    def test_hardlink_alias_matches_the_expected_installed_file(self):
        item=self.item();alias=self.root/'alias.exe'
        try:alias.hardlink_to(item['path'])
        except OSError:self.skipTest('Hard links unavailable')
        self.assertTrue(u.valid_record({**item,'path':str(alias)}))

    def test_rejects_insecure_or_foreign_redirects(self):
        redirect=u.TrustedRedirect();request=urllib.request.Request(u.MANIFEST_URL)
        for target in ['http://github.com/a','https://example.com/a','https://github.com.example.com/a']:
            with self.subTest(target=target),self.assertRaises(u.UpdateError):redirect.redirect_request(request,None,302,'',{},target)

    def test_offline_launch_keeps_settings_and_draft(self):
        own=self.item();preserved={}
        for name in ['settings.json','drafts.json']:
            path=self.root/name;path.write_bytes(b'user data');preserved[name]=path.read_bytes()
        with patch.object(u,'bundled_record',return_value=own),patch.object(u,'fetch_manifest',side_effect=OSError('offline')):
            self.assertEqual(u.managed_launch('2.0.0',self.args,lambda args:'http://existing'),'http://existing')
        self.assertEqual(u.read_json(u.STATE)['active'],own)
        for name,data in preserved.items():self.assertEqual((self.root/name).read_bytes(),data)

    def test_failed_update_falls_back_and_is_not_retried(self):
        own=self.item();new=self.item('2.1.0')
        with patch.object(u,'bundled_record',return_value=own),patch.object(u,'fetch_manifest',return_value=self.manifest()),patch.object(u,'download_release',return_value=new) as download,patch.object(u,'live_server',return_value=None),patch.object(u,'run_record',side_effect=u.UpdateError('cannot boot')) as run:
            self.assertEqual(u.managed_launch('2.0.0',self.args,lambda args:'http://old'),'http://old')
            self.assertEqual(u.read_json(u.STATE)['active'],own)
            self.assertEqual(u.read_json(u.STATE)['failed']['version'],'2.1.0')
            download.reset_mock();run.reset_mock()
            u.managed_launch('2.0.0',self.args,lambda args:'http://old')
            download.assert_not_called();run.assert_not_called()

    def test_active_report_defers_update(self):
        own=self.item();new=self.item('2.1.0')
        with patch.object(u,'bundled_record',return_value=own),patch.object(u,'fetch_manifest',return_value=self.manifest()),patch.object(u,'download_release',return_value=new),patch.object(u,'live_server',return_value={'busy':True,'url':'http://busy'}),patch.object(u,'run_record') as run:
            self.assertEqual(u.managed_launch('2.0.0',self.args,lambda args:self.fail('do not restart')),'http://busy')
            run.assert_not_called()

    def test_successful_update_records_only_ready_version(self):
        own=self.item();new=self.item('2.1.0');u.activate(own)
        with patch.object(u,'bundled_record',return_value=own),patch.object(u,'fetch_manifest',return_value=self.manifest()),patch.object(u,'download_release',return_value=new),patch.object(u,'live_server',return_value=None),patch.object(u,'run_record',return_value='http://new'):
            self.assertEqual(u.managed_launch('2.0.0',self.args,lambda args:self.fail('old launch')),'http://new')
        state=u.read_json(u.STATE)
        self.assertEqual(state['active'],new);self.assertEqual(state['previous'],own)

    def test_broken_installed_version_restores_bundled_copy(self):
        own=self.item();broken=self.item('2.9.0');u.activate(broken);self.args.managed=False
        with patch.object(u,'bundled_record',return_value=own),patch.object(u,'run_record',side_effect=[u.UpdateError('invalid exe'),'http://base']):
            self.assertEqual(u.launch('2.0.0',self.args,None),'http://base')
        state=u.read_json(u.STATE)
        self.assertEqual(state['active'],own);self.assertTrue(u.same_build(state['failed'],broken))
        self.assertNotEqual(state.get('previous'),broken)


if __name__=='__main__':unittest.main()

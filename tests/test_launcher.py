import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'local-web'))
import app_launcher as launcher


class LauncherTests(unittest.TestCase):
    def test_stale_marker_of_a_terminated_server_does_not_block_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / 'server.json'
            marker.write_text(json.dumps({'pid': 12345, 'port': 8765}))
            with patch.object(launcher, 'SERVER_INFO', marker), \
                    patch.object(launcher.urllib.request, 'urlopen', return_value=io.BytesIO(b'{}')), \
                    patch.object(launcher, 'status', return_value=False), patch.object(launcher, 'process_alive', return_value=False):
                launcher.stop_server('http://127.0.0.1:8765')
            self.assertFalse(marker.exists())

    def test_existing_server_with_an_executable_alias_is_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            exe = Path(temporary) / 'installed.exe'
            exe.write_bytes(b'program')
            alias = Path(temporary) / 'alias.exe'
            try: alias.hardlink_to(exe)
            except OSError: self.skipTest('Hard links unavailable')
            running = {'version': launcher.APP_VERSION, 'executable': str(alias), 'execution_mode': 'exe'}
            args = SimpleNamespace(port=None, installed_executable=str(exe))
            with patch.object(launcher, 'FROZEN', True), patch.object(launcher, 'select_server', return_value=(8765, running)), \
                    patch.object(launcher, 'stop_server') as stop, patch.object(launcher, 'start_server') as start, \
                    patch.object(launcher.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps({'hmi_available': True}).encode())):
                self.assertEqual(launcher.start_local(args), 'http://127.0.0.1:8765')
                stop.assert_not_called(); start.assert_not_called()

import unittest,tempfile,runpy,json
from pathlib import Path
from unittest.mock import patch,Mock
SCRIPT=Path(__file__).parents[1]/'deploy'/'nas-admin.py'
class AdminBridgeTest(unittest.TestCase):
 def exercise(self,action,fail=False):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);queue=root/'queue';queue.mkdir();conf=root/'aria.conf';conf.write_text('rpc-listen-port=6800\n')
   request=queue/(action+'.request');request.write_text('test')
   def mapped(path):return queue if path=='/var/lib/nova-download-vault/admin-control' else conf if path=='/root/.aria2/aria2.conf' else Path(path)
   response=Mock();response.read.return_value=b'{"result":"OK"}'
   calls=[]
   def command(args,**kw):
    self.assertFalse(request.exists(),'requests must be consumed before reboot')
    calls.append(args)
   with patch('pathlib.Path',side_effect=mapped),patch('urllib.request.urlopen',side_effect=RuntimeError('offline') if fail else None,return_value=response),patch('subprocess.run',side_effect=command):
    if fail:
     with self.assertRaises(RuntimeError):runpy.run_path(str(SCRIPT))
    else:runpy.run_path(str(SCRIPT))
   if action in ('nas','downloader'):
    self.assertFalse(request.exists());self.assertEqual(json.loads((queue/'last-result.json').read_text())['status'],'failed' if fail else 'accepted')
   return calls
 def test_nas_reboot_consumes_request(self):self.assertIn(['/bin/systemctl','reboot'],self.exercise('nas'))
 def test_downloader_restart_only(self):self.assertIn(['/bin/systemctl','restart','aria2.service'],self.exercise('downloader'))
 def test_save_failure_does_not_restart(self):self.assertEqual([],self.exercise('nas',True))
 def test_arbitrary_actions_ignored(self):self.assertEqual([],self.exercise('shell'))

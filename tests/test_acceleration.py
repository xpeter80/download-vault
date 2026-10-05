import unittest,tempfile,sys,json
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).parents[1]/'app'))
from server import Vault,Problem
from acceleration import trackers,DEFAULTS,SOURCES
class AccelerationTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name);self.calls=[]
  def rpc(m,*a):
   self.calls.append((m,a))
   if m in ('tellActive','tellWaiting'):return []
   return 'OK'
  self.app=Vault(p/'state',p/'files','/host',rpc=rpc)
 def tearDown(self):self.app.db.close();self.tmp.cleanup()
 def test_defaults_and_persistence(self):
  self.assertEqual(self.app.acceleration_status()['options'],DEFAULTS)
  self.app.acceleration_save({'options':{'max-concurrent-downloads':'3'},'auto':False})
  self.assertEqual(self.app.acceleration_status()['options']['max-concurrent-downloads'],'3')
  self.assertFalse(self.app.acceleration_status()['auto'])
  self.assertTrue(any(m=='saveSession' for m,a in self.calls))
 def test_invalid_options_never_reach_rpc(self):
  for options in ({'max-concurrent-downloads':0},{'max-connection-per-server':17},{'disk-cache':'9G'},{'max-overall-upload-limit':'50K\nfoo=bar'}):
   with self.assertRaises(Problem):self.app.acceleration_save({'options':options})
  self.assertEqual(self.calls,[])
 def test_failed_sources_retain_old_list(self):
  s=self.app.acceleration_status();s['trackers']=['udp://example.org:80/announce'];s['last_success']=12;self.app.acceleration_store(s)
  with patch('urllib.request.urlopen',side_effect=OSError()):r=self.app.acceleration_sync()
  self.assertEqual(r['trackers'],s['trackers']);self.assertEqual(r['last_success'],12);self.assertTrue(r['error'])
 def test_two_sources_merge_and_custom(self):
  response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);response.read.return_value=b'udp://example.org:80/announce\nhttps://tracker.example/announce\n'
  with patch('urllib.request.urlopen',return_value=response) as fetch:r=self.app.acceleration_sync()
  self.assertEqual(fetch.call_count,2);self.assertEqual(len(r['trackers']),2);self.assertTrue(r['last_success']);self.assertFalse(r['error'])
 def test_tracker_formats(self):
  self.assertEqual(trackers('javascript:bad\nhttps://a.test/announce\nhttps://a.test/announce'),['https://a.test/announce'])

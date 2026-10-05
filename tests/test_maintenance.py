import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'app'))
from server import Vault,now,uid,Problem
class CleanupTest(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.jobs=[];self.calls=[]
  self.app=Vault(self.root/'state',self.root/'files','/host',rpc=self.rpc)
  self.ident=uid();self.rel='2026-10/'+self.ident+'/movie.mp4';p=self.app.path('hidden',self.rel);p.parent.mkdir(parents=True);p.write_bytes(b'video');s=p.stat()
  self.app.db.execute('INSERT INTO tasks(id,gid,url,zone,date,created_at,status,folder) VALUES(?,?,?,?,?,?,?,?)',(self.ident,'old','https://example.test','hidden','2026-10-01',now(),'complete','2026-10'))
  self.app.db.execute('INSERT INTO files(id,task_id,rel,zone,size,dev,ino,mtime) VALUES(?,?,?,?,?,?,?,?)',(uid(),self.ident,self.rel,'hidden',s.st_size,s.st_dev,s.st_ino,s.st_mtime_ns))
  self.sidecar=p.with_name('movie.mp4.aria2');self.sidecar.write_bytes(b'control');self.host='/host/.vault/2026-10/'+self.ident
 def tearDown(self):self.app.db.close();self.temp.cleanup()
 def rpc(self,method,*args):
  self.calls.append((method,args))
  if method=='tellActive':return list(self.jobs)
  if method=='tellWaiting':return []
  if method=='forceRemove':self.jobs=[j for j in self.jobs if j['gid']!=args[0]]
  return 'OK'
 def metadata(self):self.jobs=[{'gid':'residual','dir':self.host,'files':[{'path':'[METADATA]movie'}]}]
 def test_completed_metadata_auto_removed_and_saved(self):
  self.metadata();self.app.assert_idle(self.ident);self.assertFalse(self.jobs);self.assertTrue(any(c[0]=='saveSession' for c in self.calls));self.assertTrue(self.sidecar.exists())
 def test_live_payload_protects_sidecar_and_move(self):
  self.jobs=[{'gid':'live','dir':self.host,'files':[{'path':self.host+'/movie.mp4'}]}]
  self.assertEqual([],self.app.maintenance_preview()['items'])
  with self.assertRaises(Problem):self.app.assert_idle(self.ident)
  self.assertTrue(self.sidecar.exists())
 def test_cleanup_preserves_video_and_is_idempotent(self):
  self.metadata();p=self.app.maintenance_preview();self.assertEqual(2,p['count']);r=self.app.maintenance_commit(p['id']);self.assertEqual(2,r['done']);self.assertFalse(self.sidecar.exists());self.assertTrue(self.app.path('hidden',self.rel).exists());self.assertEqual(r,self.app.maintenance_commit(p['id']))
 def test_changed_sidecar_skipped(self):
  p=self.app.maintenance_preview();self.sidecar.write_bytes(b'changed control');r=self.app.maintenance_commit(p['id']);self.assertEqual(1,r['skipped']);self.assertTrue(self.sidecar.exists())
 def test_unfinished_task_not_cleaned(self):
  self.app.db.execute("UPDATE tasks SET status='paused'");self.metadata();self.assertEqual(0,self.app.maintenance_preview()['count'])
 def test_external_modified_video_protects_everything(self):
  self.app.path('hidden',self.rel).write_bytes(b'changed');self.metadata();self.assertEqual(0,self.app.maintenance_preview()['count'])
 def test_symlink_not_deleted(self):
  self.sidecar.unlink();other=self.root/'other';other.write_bytes(b'private');self.sidecar.symlink_to(other);self.assertEqual(0,self.app.maintenance_preview()['count']);self.assertTrue(other.exists())

import unittest,tempfile,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'app'))
from server import Vault,uid,now,Problem
class RestoreTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.jobs=[];self.added=False;self.app=Vault(Path(self.tmp.name)/'state',Path(self.tmp.name)/'files','/host',rpc=self.rpc);self.id=uid();self.dir='/host/.vault/2026-10/'+self.id
  self.app.db.execute('INSERT INTO tasks(id,gid,url,zone,date,created_at,status,folder) VALUES(?,?,?,?,?,?,?,?)',(self.id,'expired','https://example.test','hidden','2026-10-01',now(),'active','2026-10'))
 def tearDown(self):self.app.db.close();self.tmp.cleanup()
 def rpc(self,method,*args):
  if method=='tellActive':return self.jobs
  if method=='tellWaiting':return []
  if method=='tellStatus':
   if args[0]=='expired':raise Problem('missing',503)
   return {'status':'paused','totalLength':'100','completedLength':'40','downloadSpeed':'0','files':[{'path':self.dir+'/lesson.mp4'}]}
  if method=='addUri':self.added=True
  return {'version':'test'}
 def row(self):return dict(self.app.db.execute('SELECT * FROM tasks WHERE id=?',(self.id,)).fetchone())
 def test_restored_gid_and_name_progress(self):
  self.jobs=[{'gid':'new','dir':self.dir,'files':[{'path':self.dir+'/lesson.mp4'}]}];self.app.sync();r=self.row();self.assertEqual(('new','paused','lesson.mp4',40,''),(r['gid'],r['status'],r['name'],r['completed'],r['error']))
 def test_ambiguous_directory_does_not_relink(self):
  self.jobs=[{'gid':'one','dir':self.dir,'files':[{'path':'one'}]},{'gid':'two','dir':self.dir,'files':[{'path':'two'}]}];self.app.sync();self.assertEqual('expired',self.row()['gid'])
 def test_neighbour_directory_does_not_relink(self):
  self.jobs=[{'gid':'new','dir':self.dir+'-other','files':[{'path':'other'}]}];self.app.sync();self.assertEqual('expired',self.row()['gid'])
 def test_submitting_does_not_duplicate_restored_task(self):
  self.jobs=[{'gid':'new','dir':self.dir,'files':[{'path':'lesson.mp4'}]}];self.app.db.execute("UPDATE tasks SET status='submitting'");self.app.sync();self.assertFalse(self.added);self.assertEqual('new',self.row()['gid'])

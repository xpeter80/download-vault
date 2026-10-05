import unittest,sys,tempfile,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'app'))
from server import Vault,Problem,uid,now
class BulkFilesTest(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.app=Vault(Path(self.temp.name)/'state',Path(self.temp.name)/'files','/host',rpc=lambda *a:'OK');self.ids=[]
  for i in range(2):
   tid=uid();fid=uid();self.ids.append(fid);self.app.db.execute('INSERT INTO tasks(id,gid,url,zone,date,created_at,status) VALUES(?,?,?,?,?,?,?)',(tid,uid(),'https://example.test','hidden','2026-10-01',now(),'complete'))
   self.app.db.execute('INSERT INTO files(id,task_id,rel,zone,size,dev,ino,mtime,tags) VALUES(?,?,?,?,?,?,?,?,?)',(fid,tid,'2026-10/'+tid+'/movie.mp4','hidden',10,1,i,1,json.dumps(['已有'])))
 def tearDown(self):self.app.db.close();self.temp.cleanup()
 def test_tags_append_preserves_existing(self):
  self.app.bulk_file_update({'file_ids':self.ids,'tags':['新标签','已有']})
  self.assertEqual([['已有','新标签']]*2,[json.loads(f['tags']) for f in self.app.rows('SELECT tags FROM files')])
 def test_favorite_bulk(self):
  self.app.bulk_file_update({'file_ids':self.ids,'action':'favorite'});self.assertEqual(2,self.app.db.execute('SELECT sum(favorite) FROM files').fetchone()[0])
  self.app.bulk_file_update({'file_ids':self.ids,'action':'unfavorite'});self.assertEqual(0,self.app.db.execute('SELECT sum(favorite) FROM files').fetchone()[0])
 def test_invalid_file_prevents_partial_update(self):
  self.app.db.execute("UPDATE files SET state='missing' WHERE id=?",(self.ids[1],))
  with self.assertRaises(Problem):self.app.bulk_file_update({'file_ids':self.ids,'action':'favorite'})
  self.assertEqual(0,self.app.db.execute('SELECT sum(favorite) FROM files').fetchone()[0])
 def test_too_many_merged_tags_prevents_partial_update(self):
  self.app.db.execute('UPDATE files SET tags=? WHERE id=?',(json.dumps([str(i) for i in range(20)]),self.ids[1]))
  with self.assertRaises(Problem):self.app.bulk_file_update({'file_ids':self.ids,'tags':['新']})
  self.assertEqual(['已有'],json.loads(self.app.db.execute('SELECT tags FROM files WHERE id=?',(self.ids[0],)).fetchone()[0]))

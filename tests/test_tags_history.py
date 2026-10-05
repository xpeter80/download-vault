import unittest,json
import test_vault
from server import Problem,Vault
class TagsHistoryTest(unittest.TestCase):
 setUp=test_vault.VaultTest.setUp
 tearDown=test_vault.VaultTest.tearDown
 rpc=test_vault.VaultTest.rpc
 file=test_vault.VaultTest.file
 operate=test_vault.VaultTest.operate
 def test_tag_saved_after_delete_and_restart(self):
  f=self.file(name='独特历史电影.mp4');self.app.set_file_tags(f['id'],['电视剧','待观看'])
  self.operate('delete_unstarred',[f]);h=self.app.file_history('待观看')['files'][0]
  self.assertEqual(h['state'],'deleted');self.assertTrue(h['deleted_at']);self.assertEqual(h['tags'],['电视剧','待观看'])
  match=self.app.previous_downloads('https://new.test/独特历史电影.mp4')[0]
  self.assertEqual(match['deleted_files'],1);self.assertEqual(match['remaining_files'],0);self.assertIn('可能相关',match['match_reason'])
  self.assertEqual(json.loads(self.app.db.execute('SELECT tags FROM files WHERE id=?',(f['id'],)).fetchone()[0]),['电视剧','待观看'])
  self.app.db.close();self.app=Vault(self.root/'state',self.root/'data','/host',rpc=self.rpc)
  restored=self.app.file_history('待观看')['files'][0]
  self.assertEqual(restored['state'],'deleted');self.assertEqual(restored['tags'],['电视剧','待观看']);self.assertEqual(restored['deleted_at'],h['deleted_at'])
 def test_task_tags_inherit_to_completed_files(self):
  self.app.validate_url=lambda url:None
  t=self.app.create_task({'url':'https://files.test/episode01.mp4','tags':['剧集','高清'],'request_key':'tag-download-001'})
  p=self.app.path('hidden',t['folder']+'/'+t['id']+'/episode01.mp4');p.write_bytes(b'video')
  self.app.index_files(t,{'files':[{'path':'/host/.vault/'+t['folder']+'/'+t['id']+'/episode01.mp4','length':'5','completedLength':'5'}]})
  self.assertEqual(self.app.snapshot()['files'][0]['tags'],['剧集','高清'])
 def test_migration_without_url_matches_name(self):
  f=self.file(name='legacy-'+'a'*24+'-original_video.mp4')
  self.app.db.execute("UPDATE tasks SET url='',source='import' WHERE id=?",(f['task_id'],))
  self.assertEqual(len(self.app.previous_downloads('https://other.test/original_video.mp4')),1)
  self.assertEqual(self.app.file_history()['files'][0]['url'],'')
 def test_related_signed_url_and_shared_duplicate_guard(self):
  self.app.validate_url=lambda url:None
  self.app.create_task({'url':'https://files.test/long_video.mp4?token=old','request_key':'signed-url-001'})
  with self.assertRaises(Problem) as cm:self.app.create_task({'url':'https://files.test/long_video.mp4?token=new','request_key':'signed-url-002'})
  self.assertEqual(cm.exception.details['code'],'DUPLICATE_DOWNLOAD')
  self.assertIn('可能相关',cm.exception.details['previous'][0]['match_reason'])
  t=self.app.create_task({'url':'https://files.test/long_video.mp4?token=new','request_key':'signed-url-002','allow_duplicate':True})
  self.assertTrue(t['id'])
 def test_tag_normalization_limits_and_literal_search(self):
  f=self.file();self.assertEqual(self.app.set_file_tags(f['id'],['HD','hd','ＨＤ'])['tags'],['HD'])
  with self.assertRaises(Problem):self.app.set_file_tags(f['id'],['x'*41])
  with self.assertRaises(Problem):self.app.set_file_tags(f['id'],['\n'])
  self.assertEqual(self.app.file_history("' OR 1=1 --")['total'],0)
if __name__=='__main__':unittest.main()

import sys,tempfile,unittest,json,threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'app'))
from server import Vault,Problem
from discovery import Scan,scan_response,expanded_template,Discovery
from explorer import ExploreError

def response(url,body='',ctype='text/html'):
 return {'url':url,'body':body.encode() if isinstance(body,str) else body,'headers':{'content-type':ctype},'is_html':'html' in ctype,'status':200}

class DiscoveryTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.v=Vault(self.root/'state',self.root/'data','/host');self.calls=[]
  self.docs={
   'https://site.test/robots.txt':('', 'text/plain'),
   'https://cdn.test/robots.txt':('', 'text/plain'),
   'https://site.test/':('<form action="/search"><input name="q" type="search"><input type="hidden" name="type" value="video"></form>','text/html'),
   'https://site.test/search?type=video&q=demo':('<a href="/detail/demo">demo film</a><a href="/logout">logout</a>','text/html'),
   'https://site.test/detail/demo':('<a href="/play/demo">demo 播放</a>','text/html'),
   'https://site.test/play/demo':('<video src="https://cdn.test/demo.mp4"></video><a href="/third">third</a>','text/html'),
   'https://cdn.test/demo.mp4':(b'\x00\x00\x00\x18ftypisom'+'x'.encode()*50,'video/mp4')}
  def fetch(url,**kwargs):
   self.calls.append((url,kwargs))
   if url not in self.docs:raise ExploreError('网站返回 HTTP 404')
   body,ctype=self.docs[url];return response(url,body,ctype)
  self.v.discovery.fetcher=fetch
 def tearDown(self):self.v.db.close();self.tmp.cleanup()
 def run_job(self,**kw):return self.v.discovery.start({'site':'https://site.test/','keyword':'demo',**kw},background=False)
 def test_browse_listing_reaches_player_despite_navigation_and_duplicate_anchors(self):
  self.docs['https://site.test/']=('<form action="/search"><input name="q"></form>'+''.join('<a href="/nav/%s">nav</a>'%n for n in range(20))+''.join('<a href="/detail/demo">demo</a>' for n in range(8)),'text/html')
  j=self.run_job(keyword='',browse=True)
  self.assertEqual(j['candidates'][0]['verification'],'media',j)
  self.assertFalse(j['endpoints'])
  self.assertEqual(sum(u=='https://site.test/detail/demo' for u,_ in self.calls),1)
 def test_percent_encoded_base64_player_url(self):
  import base64
  value=base64.b64encode(b'https%3A%2F%2Fcdn.test%2Ffilm.m3u8').decode()
  s=scan_response(response('https://site.test/play/1','<script>var player_aaaa={"encrypt":2,"url":"'+value+'"}</script>'),'')
  self.assertIn('https://cdn.test/film.m3u8',[c['url'] for c in s['candidates']])
 def test_user_search_uses_discovered_form_despite_indexing_rule(self):
  self.docs['https://site.test/robots.txt']=('User-agent: *\nDisallow: /search\n','text/plain')
  j=self.run_job();self.assertEqual(j['candidates'][0]['verification'],'media',j)
  self.assertTrue(any('用户主动站内搜索' in n for n in j['notes']))
 def test_failed_search_is_not_reported_as_successful_empty_result(self):
  del self.docs['https://site.test/search?type=video&q=demo']
  j=self.run_job();self.assertEqual(j['status'],'partial');self.assertIn('访问失败',j['message'])
 def test_media_sample_is_not_blocked_by_cdn_indexing_policy(self):
  self.docs['https://cdn.test/robots.txt']=('User-agent: *\nDisallow: /','text/plain')
  j=self.run_job();self.assertEqual(j['candidates'][0]['verification'],'media',j)
  self.assertNotIn('https://cdn.test/robots.txt',[u for u,_ in self.calls])
 def test_depth_two_and_real_media_confirmation(self):
  j=self.run_job();self.assertEqual(j['status'],'done',j);self.assertEqual(len(j['candidates']),1)
  c=j['candidates'][0];self.assertEqual(c['depth'],2);self.assertEqual(c['verification'],'media');self.assertTrue(c['downloadable']);self.assertEqual(len(c['trail']),3)
  self.assertNotIn('https://site.test/third',[u for u,_ in self.calls]);self.assertNotIn('https://site.test/logout',[u for u,_ in self.calls]);self.assertEqual(j['endpoints'][0]['method'],'站内 GET 搜索表单')
 def test_get_search_declared_by_script(self):
  s=Scan('https://site.test/','蓝色 天空');s.scan_data("fetch('/api/search?q=' + encodeURIComponent(word))")
  self.assertEqual(s.apis[0]['url'],'https://site.test/api/search?q=%E8%93%9D%E8%89%B2%20%E5%A4%A9%E7%A9%BA')
 def test_searchaction_template_and_relative_json_media(self):
  data='<script type="application/ld+json">{"@type":"SearchAction","target":"https://site.test/find?q={search_term_string}"}</script><script>var player={"play_url":"/movies/demo.mp4"}</script>'
  r=scan_response(response('https://site.test/',data),'a&b');self.assertIn('https://site.test/find?q=a%26b',[e['url'] for e in r['apis']]);self.assertIn('https://site.test/movies/demo.mp4',[c['url'] for c in r['candidates']])
 def test_opensearch_discovery(self):
  self.docs['https://site.test/']=('<link rel="search" type="application/opensearchdescription+xml" href="/search.xml">','text/html')
  self.docs['https://site.test/search.xml']=('<OpenSearchDescription><Url type="text/html" template="https://site.test/search?type=video&amp;q={searchTerms}"/></OpenSearchDescription>','application/xml')
  j=self.run_job();self.assertEqual(j['endpoints'][0]['method'],'OpenSearch 声明');self.assertEqual(j['candidates'][0]['verification'],'media')
 def test_json_search_results_follow_detail_urls(self):
  self.docs['https://site.test/search?type=video&q=demo']=(json.dumps({'results':[{'title':'demo','url':'/detail/demo'}]}),'application/json')
  self.assertEqual(self.run_job()['candidates'][0]['verification'],'media')
 def test_hls_not_downloaded_as_complete_video(self):
  self.docs['https://site.test/play/demo']=('<script>player={"url":"https://cdn.test/demo.m3u8"}</script>','text/html')
  self.docs['https://cdn.test/demo.m3u8']=('#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\npart.ts\n#EXT-X-ENDLIST','application/vnd.apple.mpegurl')
  j=self.run_job();c=j['candidates'][0];self.assertEqual(c['verification'],'manifest');self.assertFalse(c['downloadable'])
  with self.assertRaises(Problem):self.v.discovery.download({'job_id':j['id'],'link_id':c['id']})
 def test_encrypted_manifest_is_marked_protected(self):
  self.docs['https://site.test/play/demo']=('<video src="https://cdn.test/demo.m3u8"></video>','text/html')
  self.docs['https://cdn.test/demo.m3u8']=('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n','application/vnd.apple.mpegurl')
  self.assertEqual(self.run_job()['candidates'][0]['verification'],'protected')
 def test_fake_mp4_html_not_verified(self):
  self.docs['https://cdn.test/demo.mp4']=('<html>Login first</html>','text/html')
  c=self.run_job()['candidates'][0];self.assertEqual(c['verification'],'page');self.assertFalse(c['downloadable'])
 def test_robots_denial_stops_auto_access(self):
  self.docs['https://site.test/robots.txt']=('User-agent: *\nDisallow: /','text/plain')
  j=self.run_job();self.assertEqual(j['status'],'error');self.assertNotIn('https://site.test/',[u for u,_ in self.calls])
 def test_robots_wildcard_and_longest_allow(self):
  from discovery import CrawlerRobots
  r=CrawlerRobots();r.parse(['User-agent: *','Disallow: /private/*','Allow: /private/public$'])
  self.assertFalse(r.can_fetch('NovaVaultExplorer','https://site.test/private/item'))
  self.assertTrue(r.can_fetch('NovaVaultExplorer','https://site.test/private/public'))
  self.assertFalse(r.can_fetch('NovaVaultExplorer','https://site.test/private/public/extra'))
 def test_unavailable_robots_does_not_bypass_resource_error(self):
  original=self.v.discovery.fetcher
  def fetch(url,**kw):
   if url.endswith('/robots.txt'):raise ExploreError('HTTP 403',403)
   if url.endswith('/demo.mp4'):raise ExploreError('HTTP 403',403)
   return original(url,**kw)
  self.v.discovery.fetcher=fetch;j=self.run_job();self.assertEqual(j['status'],'done');self.assertEqual(j['candidates'][0]['verification'],'failed')
 def test_api_can_reference_public_external_manifest(self):
  self.docs['https://site.test/search?type=video&q=demo']=('{"results":[{"title":"demo","href":"https://cdn.test/collection.json"}]}','application/json')
  self.docs['https://cdn.test/collection.json']=('["https://cdn.test/demo.mp4"]','application/json')
  j=self.run_job();self.assertEqual(j['candidates'][0]['verification'],'media');self.assertEqual(j['candidates'][0]['depth'],1)
 def test_no_search_fallback_excludes_unrelated_media(self):
  self.docs['https://site.test/']=('<title>资源列表</title><a href="https://cdn.test/demo.mp4">demo</a><a href="https://cdn.test/other.mp4">other</a>','text/html')
  j=self.run_job();self.assertEqual([c['url'] for c in j['candidates']],['https://cdn.test/demo.mp4'])
 def test_task_history_persistence_and_restart_interruption(self):
  j=self.run_job();self.v.db.close();self.v=Vault(self.root/'state',self.root/'data','/host');self.assertEqual(self.v.discovery.get(j['id'])['candidates'][0]['verification'],'media')
  j['status']='running';self.v.discovery.save(j);self.v.discovery=Discovery(self.v);self.assertEqual(self.v.discovery.get(j['id'])['status'],'queued')
 def test_depth_validation_and_four_levels(self):
  for invalid in (0,5,True,'4',2.5):
   with self.assertRaises(Problem):self.run_job(depth=invalid)
  self.docs['https://site.test/play/demo']=('<a href="/third">demo next</a>','text/html')
  self.docs['https://site.test/third']=('<a href="/fourth">demo final</a>','text/html')
  self.docs['https://site.test/fourth']=('<video src="https://cdn.test/demo.mp4"></video><a href="/fifth">next</a>','text/html')
  shallow=self.run_job(depth=1);self.assertEqual(shallow['candidates'],[])
  deep=self.run_job(depth=4);self.assertEqual(deep['candidates'][0]['depth'],4)
  self.assertNotIn('https://site.test/fifth',[u for u,_ in self.calls])
 def test_search_precedes_crawl_and_unneeded_scripts(self):
  self.docs['https://site.test/']=('<form action="/search"><input name="q"><input type="hidden" name="type" value="video"></form><script src="/slow.js"></script><a href="/irrelevant">demo</a>','text/html')
  self.run_job();urls=[u for u,_ in self.calls]
  self.assertNotIn('https://site.test/slow.js',urls);self.assertNotIn('https://site.test/irrelevant',urls)
  self.assertLess(urls.index('https://site.test/search?type=video&q=demo'),urls.index('https://site.test/detail/demo'))
 def test_background_finishes_without_polling(self):
  import time
  entered=threading.Event();release=threading.Event();original=self.v.discovery.fetcher
  def fetch(url,**kw):
   if '/search?' in url:entered.set();release.wait(3)
   return original(url,**kw)
  self.v.discovery.fetcher=fetch
  j=self.v.discovery.start({'site':'https://site.test/','keyword':'demo'})
  self.assertTrue(entered.wait(2));release.set()
  deadline=time.monotonic()+4
  while self.v.discovery.events and time.monotonic()<deadline:time.sleep(.02)
  self.assertEqual(self.v.discovery.get(j['id'])['status'],'done')
 def test_worker_recovers_persisted_job(self):
  import time
  j=self.run_job();j['status']='running';self.v.discovery.save(j)
  recovered=Discovery(self.v,fetcher=self.v.discovery.fetcher);self.v.discovery=recovered
  recovered.recover()
  deadline=time.monotonic()+4
  while recovered.events and time.monotonic()<deadline:time.sleep(.02)
  result=recovered.get(j['id']);self.assertEqual(result['status'],'done');self.assertEqual(result['recoveries'],1);self.assertEqual(len(result['candidates']),1)
 def test_cancel_preserves_partial_results(self):
  original=self.v.discovery.fetcher
  def fetch(url,**kw):
   if '/search?' in url:
    for e in self.v.discovery.events.values():e.set()
   return original(url,**kw)
  self.v.discovery.fetcher=fetch;j=self.run_job();self.assertEqual(j['status'],'cancelled');self.assertGreater(len(j['pages']),0)
 def test_search_template_and_request_budget(self):
  self.assertIsNone(expanded_template('https://site.test/search','demo','https://site.test/'))
  with self.assertRaises(Problem):self.run_job(template='file:///tmp/{query}')
  with patch('discovery.MAX_REQUESTS',2):j=self.run_job()
  self.assertEqual(j['status'],'partial');self.assertLessEqual(j['requests'],2)
 def test_duplicate_reminder_reused_on_auto_download(self):
  j=self.run_job();c=j['candidates'][0]
  self.v.db.execute('INSERT INTO tasks(id,url,status,date,request_key) VALUES(?,?,?,?,?)',('previous',c['url'],'complete','2026-10-01','previous-key-123'))
  self.assertEqual(self.v.discovery.get(j['id'])['candidates'][0]['previous'][0]['status'],'complete')
  with self.assertRaises(Problem) as cm:self.v.discovery.download({'job_id':j['id'],'link_id':c['id'],'request_key':'new-key-12345'})
  self.assertEqual(cm.exception.details['code'],'DUPLICATE_DOWNLOAD')

 def test_publication_date_boundary_and_unknown(self):
  self.docs['https://site.test/play/demo']=('<meta property="article:published_time" content="2026-10-02T00:00:00+08:00"><video src="https://cdn.test/demo.mp4">','text/html')
  self.assertEqual(len(self.run_job(published_after='2026-10-01')['candidates']),1)
  self.assertEqual(len(self.run_job(published_after='2026-10-02')['candidates']),0)
  self.docs['https://site.test/play/demo']=('<video src="https://cdn.test/demo.mp4">','text/html')
  # Existing trustworthy publication evidence remains in the ledger.
  self.assertEqual(len(self.run_job(published_after='2026-10-01')['candidates']),1)
  with self.v.lock:self.v.db.execute('DELETE FROM discovery_resources')
  no_date=self.run_job(published_after='2026-10-01');self.assertEqual(no_date['candidates'],[]);self.assertEqual(no_date['unknown_dates'],1)
  kept=self.run_job(published_after='2026-10-01',include_unknown=True);self.assertEqual(len(kept['candidates']),1);self.assertIsNone(kept['candidates'][0]['published_at'])
 def test_publication_inputs(self):
  for date in ('2026-02-30','yesterday','2026-10-02T00:00:00',123):
   with self.assertRaises(Problem):self.run_job(published_after=date)
  with self.assertRaises(Problem):self.run_job(include_unknown='false')
  with self.assertRaises(Problem):self.run_job(mode='incremental',incremental_since='wrong')
  with self.assertRaises(Problem):self.run_job(mode='incremental',incremental_since='2999-01-01T00:00:00+08:00')
 def test_json_item_dates_remain_independent(self):
  self.docs['https://site.test/search?type=video&q=demo']=(json.dumps({'results':[{'url':'https://cdn.test/demo.mp4','datePublished':'2026-10-01'},{'url':'https://cdn.test/new.mp4','datePublished':'2026-10-02'}]}),'application/json')
  self.docs['https://cdn.test/new.mp4']=(b'0000ftypisom','video/mp4')
  j=self.run_job(published_after='2026-10-01');self.assertEqual([c['url'] for c in j['candidates']],['https://cdn.test/new.mp4'])
 def test_incremental_discovers_new_episode_on_old_page(self):
  old=self.run_job()
  self.docs['https://site.test/play/demo']=('<video src="https://cdn.test/demo.mp4"></video><video src="https://cdn.test/episode2.mp4"></video>','text/html')
  self.docs['https://cdn.test/episode2.mp4']=(b'0000ftypisom','video/mp4')
  j=self.run_job(mode='incremental',baseline_id=old['id'])
  self.assertEqual(j['skipped_known'],1);self.assertEqual([c['url'] for c in j['candidates']],['https://cdn.test/episode2.mp4'])
  self.assertGreater(j['candidates'][0]['first_seen'],old['finished_at'])
  next_job=self.run_job(mode='incremental');self.assertEqual(next_job['candidates'],[]);self.assertEqual(next_job['skipped_known'],2)
 def test_incremental_needs_matching_baseline(self):
  with self.assertRaises(Problem):self.run_job(mode='incremental')
  old=self.run_job()
  with self.assertRaises(Problem):self.v.discovery.start({'site':'https://site.test/','keyword':'different','mode':'incremental','baseline_id':old['id']},background=False)
 def test_incremental_ledger_survives_history_pruning_and_restart(self):
  old=self.run_job()
  self.v.db.execute('DELETE FROM discovery_jobs')
  self.v.discovery=Discovery(self.v,fetcher=self.v.discovery.fetcher)
  j=self.run_job(mode='incremental');self.assertEqual(j['candidates'],[]);self.assertEqual(j['skipped_known'],1);self.assertEqual(j['baseline_id'],old['id'])
 def test_incremental_explicit_timestamp(self):
  old=self.run_job()
  j=self.run_job(mode='incremental',incremental_since=old['finished_at']);self.assertEqual(j['candidates'],[])
  j=self.run_job(mode='incremental',incremental_since='2000-01-01T00:00:00+08:00');self.assertEqual(len(j['candidates']),1)
 def test_metadata_ignores_modification_dates_and_ambiguous_list_dates(self):
  from discovery_store import page_publication,timestamp
  self.assertEqual(page_publication('<meta property="article:modified_time" content="2026-10-02">'),{})
  data='<script type="application/ld+json">[{"@type":"VideoObject","datePublished":"2026-10-01"},{"@type":"VideoObject","datePublished":"2026-10-02"}]</script>'
  self.assertEqual(page_publication(data),{})
  self.assertEqual(timestamp('2026-10-02T00:00:00+08:00'),'2026-10-01T16:00:00+00:00')
 def test_old_job_migration_backfills_known_resources(self):
  old=self.run_job();self.v.db.execute('DELETE FROM discovery_resources');self.v.db.execute('DELETE FROM discovery_checkpoints');self.v.db.execute("DELETE FROM settings WHERE key='discovery_ledger_v1'")
  self.v.discovery=Discovery(self.v,fetcher=self.v.discovery.fetcher)
  j=self.run_job(mode='incremental',baseline_id=old['id']);self.assertEqual(j['candidates'],[]);self.assertEqual(j['skipped_known'],1)

 def test_incremental_prioritizes_new_episode_pages(self):
  self.docs['https://site.test/detail/demo']=(''.join('<a href="/play/episode%d">demo 播放 %d</a>'%(i,i) for i in range(6)),'text/html')
  for i in range(7):
   self.docs['https://site.test/play/episode%d'%i]=('<video src="https://cdn.test/episode%d.mp4"></video>'%i,'text/html')
   self.docs['https://cdn.test/episode%d.mp4'%i]=(b'0000ftypisom','video/mp4')
  old=self.run_job();self.assertEqual(len(old['candidates']),6)
  self.docs['https://site.test/detail/demo']=(self.docs['https://site.test/detail/demo'][0]+'<a href="/play/episode6">demo 播放 6</a>','text/html')
  j=self.run_job(mode='incremental',baseline_id=old['id']);self.assertEqual([c['url'] for c in j['candidates']],['https://cdn.test/episode6.mp4'])

 def test_api_magnet_publication_and_time_metadata(self):
  from discovery_store import page_publication
  magnet='magnet:?xt=urn:btih:'+'a'*40
  self.docs['https://site.test/search?type=video&q=demo']=(json.dumps({'results':[{'url':magnet,'datePublished':'2026-10-02'}]}),'application/json')
  j=self.run_job(published_after='2026-10-01');self.assertEqual(j['candidates'][0]['kind'],'magnet');self.assertIsNotNone(j['candidates'][0]['published_at'])
  self.assertEqual(page_publication('<time itemprop="datePublished" datetime="2026-10-02"></time>')['published_at'],'2026-10-01T16:00:00+00:00')

 def test_image_candidates_available_without_crowding_media(self):
  self.docs['https://site.test/play/demo']=(''.join('<img src="https://cdn.test/image%d.jpg">'%i for i in range(30))+'<video src="https://cdn.test/demo.mp4"></video>','text/html')
  j=self.run_job();self.assertEqual(len([c for c in j['candidates'] if '.jpg' in c['url']]),20)
  self.assertTrue(any(c['verification']=='media' for c in j['candidates']))

if __name__=='__main__':unittest.main()

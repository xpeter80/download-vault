import sys,tempfile,unittest,json,threading,http.server,http.client
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'app'))
from server import Vault,Problem,Handler
from explorer import Reader,parse_page,normalize_url,public_ips,fetch_public,ExploreError
MAG='magnet:?xt=urn:btih:'+'a'*40+'&dn=sample'
HTML='''<!doctype html><html><head><title>资源目录</title><base href="https://example.com/releases/"><script>alert(1)</script></head><body><h1>文件列表</h1><a href="demo.zip">下载压缩包</a><a href="demo.zip">重复</a><a download="guide.pdf" href="/get?id=123">指南</a><a href="next.html">下一页</a><a href="'''+MAG+'''">样例磁力</a><video src="/media?id=12"></video><img src="https://tracker.example/1" onerror="alert(2)" alt="海报"><script>window.location='https://evil.example';</script><iframe src="https://evil.example"></iframe><svg onload="alert(3)"></svg><form action="https://evil.example"><input name="password"></form><a href="javascript:alert(4)">恶意</a><a href="/sample.torrent">BT 种子</a><p>&lt;script&gt;文本&lt;/script&gt;</p></body></html>'''
def page(url='https://example.com/page',body=HTML):return {'url':url,'headers':{'content-type':'text/html; charset=utf-8'},'body':body.encode(),'is_html':True,'status':200}
class ExplorerTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
  self.app=Vault(self.base/'state',self.base/'files','/host',gateway_key='test',origin='https://vault.test',rpc=lambda *a:'ok')
  self.app.explorer.fetcher=lambda url,**kw:page(url)
 def tearDown(self):self.app.db.close();self.tmp.cleanup()
 def test_extract_resolves_deduplicates_and_names(self):
  r=parse_page(page());self.assertEqual(r['title'],'资源目录');cs=r['candidates'];urls=[x['url'] for x in cs]
  self.assertEqual(urls.count('https://example.com/releases/demo.zip'),1);self.assertIn('https://example.com/get?id=123',urls);self.assertIn('https://example.com/media?id=12',urls);self.assertIn(MAG,urls)
  self.assertTrue(any(x['kind']=='torrent' for x in cs));self.assertTrue(any(x['url'].endswith('next.html') for x in r['links']))
 def test_reader_strips_active_content_and_remote_resources(self):
  s=parse_page(page())['html']
  for bad in ('<script','<iframe','<svg','<img','<form','<input','onerror=','onload=','javascript:','window.location'):self.assertNotIn(bad,s)
  self.assertIn('&lt;script&gt;',s);self.assertIn('data-explore-nav=',s)
 def test_reader_layout_cannot_override_app_navigation(self):
  s=parse_page(page(body='<main><header>heading</header><nav><a href="/next">next</a></nav><footer>end</footer></main>'))['html']
  for tag in ('main','nav','header','footer'):self.assertNotIn('<'+tag,s)
  self.assertIn('data-explore-nav=',s)
 def test_encoded_and_plain_magnets(self):
  from urllib.parse import quote
  s='<a href="'+quote(MAG,safe='')+'">磁力</a><script>const url="'+MAG+'"</script>'
  cs=parse_page(page(body=s))['candidates'];self.assertEqual(len(cs),1);self.assertEqual(cs[0]['url'],MAG)
 def test_non_html_returns_file_without_rendering(self):
  r=parse_page({'url':'https://example.com/file','headers':{'content-type':'application/octet-stream'},'body':b'','is_html':False});self.assertTrue(r['candidates'][0]['verified'])
 def test_chinese_gbk_page(self):
  r=page();r['headers']['content-type']='text/html; charset=gb2312';r['body']='<title>中文标题</title><p>内容</p>'.encode('gbk');self.assertEqual(parse_page(r)['title'],'中文标题')
 def test_history_persists_and_counts_revisits(self):
  a=self.app.explorer.visit('https://example.com/page');b=self.app.explorer.visit('https://example.com/page');self.assertEqual(a['id'],b['id']);self.assertEqual(self.app.explorer.history()[0]['visit_count'],2)
  self.app.db.close();self.app=Vault(self.base/'state',self.base/'files','/host');self.assertEqual(self.app.explorer.saved(a['id'])['title'],'资源目录')
 def test_failed_visit_is_recorded(self):
  def failed(url):raise ExploreError('网站需要验证')
  self.app.explorer.fetcher=failed;r=self.app.explorer.visit('https://example.com/');self.assertEqual(r['status'],'error');self.assertEqual(self.app.explorer.history()[0]['error'],'网站需要验证')
 def test_download_uses_existing_task_api_and_visibility(self):
  p=self.app.explorer.visit('https://example.com/');c=next(c for c in p['candidates'] if c['kind']=='magnet');calls=[]
  self.app.create_task=lambda d:(calls.append(d) or {'id':'task','status':'waiting'})
  out=self.app.explorer.download({'page_id':p['id'],'link_id':c['id'],'hidden':False,'request_key':'test-request-123'});self.assertEqual(out['id'],'task');self.assertFalse(calls[0]['hidden']);self.assertEqual(calls[0]['url'],MAG)
 def test_html_download_button_rejected(self):
  p=self.app.explorer.visit('https://example.com/');c=next(c for c in p['candidates'] if c['kind']=='direct')
  with self.assertRaises(Problem):self.app.explorer.download({'page_id':p['id'],'link_id':c['id'],'request_key':'test-request-123'})
 def test_direct_probe_then_download(self):
  p=self.app.explorer.visit('https://example.com/');c=next(c for c in p['candidates'] if c['kind']=='direct');calls=[]
  self.app.explorer.fetcher=lambda *a,**kw:{'is_html':False}
  self.app.create_task=lambda d:(calls.append(d) or {'id':'task','status':'waiting'})
  self.app.explorer.download({'page_id':p['id'],'link_id':c['id'],'request_key':'test-request-123'});self.assertEqual(calls[0]['url'],c['url'])
 def test_retry_returns_task_without_reprobing_expired_url(self):
  p=self.app.explorer.visit('https://example.com/');c=next(c for c in p['candidates'] if c['kind']=='direct')
  self.app.db.execute('INSERT INTO tasks(id,url,status,request_key) VALUES(?,?,?,?)',('existing',c['url'],'complete','retry-request-123'))
  def expired(*a,**kw):raise ExploreError('expired')
  self.app.explorer.fetcher=expired
  r=self.app.explorer.download({'page_id':p['id'],'link_id':c['id'],'request_key':'retry-request-123'});self.assertEqual(r['id'],'existing')
 def test_previous_download_badge_survives_file_cleanup(self):
  p=self.app.explorer.visit('https://example.com/');c=p['candidates'][0]
  self.app.db.execute('INSERT INTO tasks(id,url,status,date,request_key) VALUES(?,?,?,?,?)',('old',c['url'],'complete','2026-10-01','old-request-123'))
  fresh=self.app.explorer.saved(p['id']);prior=fresh['candidates'][0]['previous'][0]
  self.assertEqual(prior['status'],'complete');self.assertEqual(prior['remaining_files'],0)
 def test_magnet_identity_ignores_trackers_and_name(self):
  from explorer import download_identity
  import base64
  a='magnet:?xt=urn:btih:'+'a'*40+'&dn=first&tr=https://one.example'
  b='magnet:?dn=second&xt=urn:btih:'+base64.b32encode(bytes.fromhex('a'*40)).decode()+'&tr=https://two.example'
  self.assertEqual(download_identity(a),download_identity(b))
  self.assertNotEqual(download_identity('https://example.com/file?id=1'),download_identity('https://example.com/file?id=2'))
 def test_repeat_download_requires_explicit_confirmation(self):
  self.app.db.execute('INSERT INTO tasks(id,url,status,date,request_key) VALUES(?,?,?,?,?)',('old',MAG,'complete','2026-10-01','old-request-123'))
  with self.assertRaises(Problem) as caught:self.app.create_task({'url':MAG,'request_key':'new-request-123'})
  self.assertEqual(caught.exception.status,409);self.assertEqual(caught.exception.details['code'],'DUPLICATE_DOWNLOAD')
  def rpc(method,*args):
   if method in ('tellActive','tellWaiting'):return []
   if method=='tellStatus':raise Problem('not found')
   return 'gid'
  self.app.rpc=rpc
  task=self.app.create_task({'url':MAG,'request_key':'new-request-123','allow_duplicate':True})
  self.assertNotEqual(task['id'],'old')
  self.assertEqual(self.app.create_task({'url':MAG,'request_key':'new-request-123'})['id'],task['id'])
 def test_history_removal_does_not_touch_downloads(self):
  r=self.app.explorer.visit('https://example.com/');self.app.explorer.remove(r['id']);self.assertEqual(self.app.explorer.history(),[])
  with self.assertRaises(Problem):self.app.explorer.saved(r['id'])
 def test_url_validation(self):
  self.assertEqual(normalize_url('example.com/a#part'),'https://example.com/a')
  for u in ('file:///etc/passwd','https://user:password@example.com','http://example.com:6868','https://example.com\nheader','javascript:alert(1)'):
   with self.assertRaises(ExploreError):normalize_url(u)
 def test_private_dns_rejected(self):
  for ip in ('127.0.0.1','192.168.50.130','169.254.169.254','::1','224.0.0.1'):
   with patch('explorer.socket.getaddrinfo',return_value=[(2,1,6,'',(ip,443))]):
    with self.assertRaises(ExploreError):public_ips('example.com',443)
 def test_redirect_revalidated_before_connection(self):
  class Response:
   status=302
   def getheaders(self):return [('Location','http://127.0.0.1/private')]
  class Connection:
   def __init__(self,*a):self.args=a
   def request(self,*a,**k):pass
   def getresponse(self):return Response()
   def close(self):pass
  def resolver(host,port):
   if host=='127.0.0.1':raise ExploreError('private')
   return ['93.184.216.34']
  with patch('explorer.public_ips',side_effect=resolver),patch('explorer.PinnedHTTPS',Connection),patch('explorer.PinnedHTTP',Connection):
   with self.assertRaises(ExploreError):fetch_public('https://example.com/')
 def test_history_api_requires_auth_and_visit_requires_origin(self):
  srv=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);srv.app=self.app;threading.Thread(target=srv.serve_forever,daemon=True).start()
  try:
   c=http.client.HTTPConnection('127.0.0.1',srv.server_port);c.request('GET','/api/explore/history');self.assertEqual(c.getresponse().status,401);c.close()
   c=http.client.HTTPConnection('127.0.0.1',srv.server_port);c.request('POST','/api/explore/visit',body=json.dumps({'url':'https://example.com'}),headers={'X-Vault-Gateway':'test','Content-Type':'application/json','Origin':'https://evil.test'});self.assertEqual(c.getresponse().status,403);c.close()
  finally:srv.shutdown();srv.server_close()
if __name__=='__main__':unittest.main(verbosity=2)

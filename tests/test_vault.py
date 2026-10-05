import sys,tempfile,unittest,threading,json,urllib.request,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'app'))
from server import Vault,Problem,now,uid,move_no_replace,Handler
import http.server

class VaultTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.active=[];self.waiting=[];self.offline=False
  self.app=Vault(self.root/'state',self.root/'data','/host',rpc=self.rpc,gateway_key='test-gateway',origin='https://vault.test')
 def tearDown(self):self.app.db.close();self.tmp.cleanup()
 def rpc(self,m,*args):
  if self.offline:raise Problem('offline',503)
  if m=='getVersion':return {'version':'test'}
  if m=='tellActive':return self.active
  if m=='tellWaiting':return self.waiting
  if m=='tellStatus':raise Problem('missing',502)
  if m=='addUri':return args[1]['gid']
  return 'OK'
 def file(self,date='2026-10-02',zone='hidden',name='电影.mp4'):
  ident=uid();rel=date+'/'+ident+'/'+name;p=self.app.path(zone,rel);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'data');s=p.stat();fid=uid()
  self.app.db.execute('INSERT INTO tasks(id,gid,url,zone,date,created_at,status) VALUES(?,?,?,?,?,?,?)',(ident,uid()[:16],'https://example.com',zone,date,now(),'complete'))
  self.app.db.execute('INSERT INTO files(id,task_id,rel,zone,size,dev,ino,mtime) VALUES(?,?,?,?,?,?,?,?)',(fid,ident,rel,zone,s.st_size,s.st_dev,s.st_ino,s.st_mtime_ns))
  return dict(self.app.db.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone())
 def operate(self,action,fs):
  p=self.app.preview({'action':action,'file_ids':[f['id'] for f in fs]});self.app.commit(p['id']);self.app.run_ops();return p
 def test_roundtrip_preserves_favorite_and_visible(self):
  a=self.file();b=self.file(zone='visible');self.app.favorite(a['id'],True)
  self.operate('expose',[a,b]);s=self.app.snapshot();fa=next(f for f in s['files'] if f['id']==a['id']);self.assertEqual(fa['zone'],'temporary');self.assertEqual(fa['favorite'],1)
  self.operate('hide',s['files']);self.assertTrue(self.app.path('hidden',a['rel']).exists());self.assertTrue(self.app.path('visible',b['rel']).exists())
 def test_cleanup_protects_favorites_and_unmanaged(self):
  a=self.file();b=self.file();self.app.favorite(a['id'],True);unmanaged=self.app.path('hidden','unmanaged.txt');unmanaged.write_text('keep')
  self.operate('delete_unstarred',[a,b]);self.assertTrue(self.app.path('hidden',a['rel']).exists());self.assertFalse(self.app.path('hidden',b['rel']).exists());self.assertTrue(unmanaged.exists())
 def test_favorite_after_preview_invalidates(self):
  a=self.file();p=self.app.preview({'action':'delete_unstarred','file_ids':[a['id']]});self.app.favorite(a['id'],True)
  with self.assertRaises(Problem):self.app.commit(p['id'])
 def test_active_task_never_moves(self):
  a=self.file();self.active=[{'gid':'other','dir':'/host/.vault/'+str(Path(a['rel']).parent)}];p=self.operate('expose',[a]);self.assertTrue(self.app.path('hidden',a['rel']).exists());self.assertEqual(self.app.snapshot()['batches'][0]['status'],'partial')
 def test_rpc_failure_never_deletes(self):
  a=self.file();self.offline=True;self.operate('delete_unstarred',[a]);self.assertTrue(self.app.path('hidden',a['rel']).exists())
 def test_collision_never_overwrites(self):
  a=self.file();p=self.app.path('temporary',a['rel']);p.parent.mkdir(parents=True);p.write_text('original');self.operate('expose',[a]);self.assertEqual(p.read_text(),'original');self.assertTrue(self.app.path('hidden',a['rel']).exists())
 def test_restart_after_rename_recovers(self):
  a=self.file();b=self.app.preview({'action':'expose','file_ids':[a['id']]});self.app.commit(b['id']);self.app.db.execute("UPDATE ops SET status='intent'");dst=self.app.path('temporary',a['rel']);dst.parent.mkdir(parents=True);move_no_replace(self.app.path('hidden',a['rel']),dst)
  self.app.db.close();self.app=Vault(self.root/'state',self.root/'data','/host',rpc=self.rpc);self.app.run_ops();self.assertEqual(self.app.snapshot()['files'][0]['zone'],'temporary');self.assertEqual(self.app.snapshot()['batches'][0]['status'],'done')
 def test_restart_after_unlink_recovers(self):
  a=self.file();b=self.app.preview({'action':'delete_unstarred','file_ids':[a['id']]});self.app.commit(b['id']);self.app.db.execute("UPDATE ops SET status='intent'");self.app.path('hidden',a['rel']).unlink();self.app.run_ops();self.assertEqual(self.app.snapshot()['files'],[])
 def test_symlink_and_traversal_rejected(self):
  with self.assertRaises(Problem):self.app.path('hidden','../../secret')
  (self.root/'data'/'.vault'/'escape').symlink_to(self.root)
  with self.assertRaises(Problem):self.app.path('hidden','escape/test')
 def test_external_modification_protected(self):
  a=self.file();self.app.path('hidden',a['rel']).write_text('changed');self.operate('delete_unstarred',[a]);self.assertTrue(self.app.path('hidden',a['rel']).exists())
 def test_idempotent_commit(self):
  a=self.file();p=self.app.preview({'action':'expose','file_ids':[a['id']]});self.app.commit(p['id']);self.app.commit(p['id']);self.assertEqual(self.app.db.execute('SELECT count(*) FROM ops').fetchone()[0],1)
 def test_settings_locked_after_task(self):
  self.file()
  with self.assertRaises(Problem):self.app.settings({'hidden':'.new','visible':'public','temporary':'tmp'})
 def test_concurrent_batch_rejected(self):
  a=self.file();p=self.app.preview({'action':'expose','file_ids':[a['id']]});q=self.app.preview({'action':'delete_unstarred','file_ids':[a['id']]});self.app.commit(p['id'])
  with self.assertRaises(Problem):self.app.commit(q['id'])
 def test_http_auth_and_csrf(self):
  srv=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);srv.app=self.app;threading.Thread(target=srv.serve_forever,daemon=True).start();url='http://127.0.0.1:'+str(srv.server_port)
  try:
   with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(url+'/api/state')
   self.assertEqual(e.exception.code,401)
   req=urllib.request.Request(url+'/api/settings',data=b'{}',headers={'X-Vault-Gateway':'test-gateway','Content-Type':'application/json','Origin':'https://evil.test'})
   with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(req)
   self.assertEqual(e.exception.code,403)
   req=urllib.request.Request(url+'/api/state',headers={'X-Vault-Gateway':'test-gateway'})
   with urllib.request.urlopen(req) as r:self.assertEqual(r.status,200)
  finally:srv.shutdown();srv.server_close()
 def test_owner_auth_accepts_owner_rejects_other_user(self):
  class Identity(http.server.BaseHTTPRequestHandler):
   def do_GET(self):
    self.send_response(200 if self.headers.get('Cookie')=='session=owner' else 403);self.end_headers()
   def log_message(self,*args):pass
  identity=http.server.ThreadingHTTPServer(('127.0.0.1',0),Identity)
  threading.Thread(target=identity.serve_forever,daemon=True).start()
  self.app.auth_url='http://127.0.0.1:'+str(identity.server_port)+'/verify-owner'
  srv=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);srv.app=self.app
  threading.Thread(target=srv.serve_forever,daemon=True).start();url='http://127.0.0.1:'+str(srv.server_port)+'/api/state'
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers={'Cookie':'session=owner'})) as r:self.assertEqual(r.status,200)
   with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(urllib.request.Request(url,headers={'Cookie':'session=other'}))
   self.assertEqual(e.exception.code,403)
  finally:srv.shutdown();srv.server_close();identity.shutdown();identity.server_close()
 def test_nested_download_keeps_tree_on_roundtrip(self):
  f=self.file(name='season/episode/file.mkv');self.operate('expose',[f]);self.assertTrue(self.app.path('temporary',f['rel']).exists());self.operate('hide',self.app.snapshot()['files']);self.assertTrue(self.app.path('hidden',f['rel']).exists())
 def test_login_expiry_routing_and_error_classification(self):
  class Identity(http.server.BaseHTTPRequestHandler):
   def do_GET(self):
    cookie=self.headers.get('Cookie','')
    code,message=(403,'登录续期失败，请重新进入应用') if cookie=='session=expired' else ((502,'upstream failure') if cookie=='session=outage' else (403,'owner only'))
    body=json.dumps({'error':message}).encode();self.send_response(code);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
   def log_message(self,*args):pass
  identity=http.server.ThreadingHTTPServer(('127.0.0.1',0),Identity);threading.Thread(target=identity.serve_forever,daemon=True).start()
  self.app.auth_url='http://127.0.0.1:'+str(identity.server_port)+'/verify-owner'
  srv=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);srv.app=self.app;threading.Thread(target=srv.serve_forever,daemon=True).start()
  def call(path,cookie=''):
   c=http.client.HTTPConnection('127.0.0.1',srv.server_port);c.request('GET',path,headers={'Cookie':cookie});r=c.getresponse();result=(r.status,dict(r.getheaders()),r.read());c.close();return result
  try:
   status,headers,body=call('/','session=expired');self.assertEqual(status,303);self.assertEqual(headers['Location'],'/api/auth/start?return=%2Fdownloads%2F')
   status,headers,body=call('/api/state','session=expired');self.assertEqual(status,401);self.assertEqual(json.loads(body)['code'],'LOGIN_REQUIRED');self.assertNotIn('Location',headers)
   self.assertEqual(call('/api/state','session=outage')[0],503)
   self.assertEqual(call('/api/state','session=member')[0],403)
   status,headers,body=call('/','session=member');self.assertEqual(status,403);self.assertIn('text/html',headers['Content-Type']);self.assertIn(b'href="login"',body)
   self.assertEqual(call('/style.css')[0],200)
   self.assertEqual(call('/login?return=https://evil.test')[1]['Location'],'/api/auth/start?return=%2Fdownloads%2F')
   self.assertEqual(call('/api/state')[0],401)
  finally:srv.shutdown();srv.server_close();identity.shutdown();identity.server_close()
if __name__=='__main__':unittest.main(verbosity=2)

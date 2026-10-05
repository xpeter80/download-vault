"""Loopback-only UI test harness. Never included in the production Docker image."""
import tempfile,sys,threading,http.server
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'app'))
from server import Vault,Handler,uid,now
sandbox=tempfile.TemporaryDirectory(prefix='vault-ui-')
a=Vault(Path(sandbox.name)/'state',Path(sandbox.name)/'data','/test',rpc=lambda method,*args: [] if method in ('tellActive','tellWaiting') else {'version':'test'},origin='http://127.0.0.1:8794')
for i,(name,zone,date) in enumerate([('旅行纪录片.mp4','hidden','2026-10-02'),('设计素材.zip','hidden','2026-10-01'),('家庭相册.zip','visible','2026-09-30')]):
 tid=uid();fid=uid();rel=date+'/'+tid+'/'+name;p=a.path(zone,rel);p.parent.mkdir(parents=True);p.write_text('test fixture');s=p.stat()
 a.db.execute('INSERT INTO tasks(id,gid,url,zone,date,created_at,status,name) VALUES(?,?,?,?,?,?,?,?)',(tid,uid()[:16],'https://example.com/test',zone,date,now(),'complete',name))
 a.db.execute('INSERT INTO files(id,task_id,rel,zone,favorite,size,dev,ino,mtime) VALUES(?,?,?,?,?,?,?,?,?)',(fid,tid,rel,zone,int(i==0),s.st_size,s.st_dev,s.st_ino,s.st_mtime_ns))
real_fetch=a.explorer.fetcher
def fixture_fetch(url,**kw):
 if url=='https://example.com/resources':
  return {'url':url,'headers':{'content-type':'text/html'},'body':b'<title>Download reminder test</title><a download href="https://example.com/test">Already downloaded</a>','is_html':True,'status':200}
 return real_fetch(url,**kw)
a.explorer.fetcher=fixture_fetch
real_discovery_fetch=a.discovery.fetcher
auto_docs={
 'https://demo.example/robots.txt':('', 'text/plain'),
 'https://demo.example/':('<title>公开课程资源</title><form action="/search"><input type="search" name="q"></form>', 'text/html'),
 'https://demo.example/search?q=demo':('<title>demo 搜索结果</title><a href="/detail/demo">demo 课程详情</a>', 'text/html'),
 'https://demo.example/detail/demo':('<title>demo 课程详情</title><a href="/play/demo">播放课程</a>', 'text/html'),
 'https://demo.example/play/demo':('<title>demo 播放页</title><video src="/demo.mp4"></video><source src="/demo.m3u8">', 'text/html'),
 'https://demo.example/demo.mp4':(b'\x00\x00\x00\x18ftypisom'+b'x'*40, 'video/mp4'),
 'https://demo.example/demo.m3u8':('#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\npart.ts\n#EXT-X-ENDLIST', 'application/vnd.apple.mpegurl')}
auto_visits=0
def auto_fixture_fetch(url,**kw):
 global auto_visits
 if url=='https://demo.example/':auto_visits+=1
 if url=='https://demo.example/episode2.mp4':return {'url':url,'body':b'0000ftypisom','headers':{'content-type':'video/mp4'},'is_html':False,'status':200}
 if url in auto_docs:
  body,ctype=auto_docs[url]
  if url=='https://demo.example/play/demo':
   body='<meta property="article:published_time" content="2026-10-02T08:00:00+08:00">'+body
   if auto_visits>1:body+='<video src="/episode2.mp4"></video>'
  return {'url':url,'headers':{'content-type':ctype},'body':body.encode() if isinstance(body,str) else body,'is_html':'html' in ctype,'status':200}
 return real_discovery_fetch(url,**kw)
a.discovery.fetcher=auto_fixture_fetch
class Local(Handler):
 def authorize(self):
  assert self.client_address[0]=='127.0.0.1'
s=http.server.ThreadingHTTPServer(('127.0.0.1',8794),Local);s.app=a
threading.Thread(target=a.worker,daemon=True).start()
print('Loopback UI fixtures ready',flush=True)
s.serve_forever()

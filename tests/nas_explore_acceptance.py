"""Real Explorer -> Aria2 test; isolated state and files, never production data."""
import sys,json,time,hashlib,urllib.request
from pathlib import Path
sys.path.insert(0,'/app')
from server import Vault,uid
c=json.loads(Path('/private/config.json').read_text());c.update(state='/test-state',storage='/test-data',host_storage='/home/nas3/download/.nova-vault-explore-acceptance-20261002')
a=Vault(**c)
try:
 p=a.explorer.visit('https://releases.ubuntu.com/24.04/')
 assert p['status']=='ok',p['error']
 assert any(x['kind']=='torrent' for x in p['candidates'])
 print('PASS: public page reading and direct/torrent extraction; candidates='+str(len(p['candidates'])),flush=True)
 url='https://releases.ubuntu.com/24.04/SHA256SUMS'
 p=a.explorer.visit(url);assert p['status']=='ok',p['error']
 task=a.explorer.download({'page_id':p['id'],'link_id':p['candidates'][0]['id'],'hidden':True,'request_key':uid()})
 deadline=time.time()+75
 while time.time()<deadline:
  a.sync()
  if a.snapshot()['files']:break
  time.sleep(2)
 fs=a.snapshot()['files'];assert len(fs)==1,'download did not complete'
 f=fs[0];assert f['zone']=='hidden'
 source=urllib.request.urlopen(url,timeout=15).read()
 assert hashlib.sha256(source).digest()==hashlib.sha256(a.path(f['zone'],f['rel']).read_bytes()).digest()
 print('PASS: Explorer one-click download via real Aria2, hidden directory and SHA-256',flush=True)
 a.db.close();a=Vault(**c);assert len(a.explorer.history())==2;assert a.explorer.saved(p['id'])['url']==url
 print('PASS: browsing history and page snapshot survive database reopen',flush=True)
 b=a.preview({'action':'delete_unstarred','file_ids':[f['id']]});a.commit(b['id']);a.run_ops();assert not a.snapshot()['files']
 print('PASS: isolated downloaded file cleaned up',flush=True)
finally:
 for t in a.rows('SELECT gid FROM tasks'):
  try:a.rpc('removeDownloadResult',t['gid'])
  except Exception:pass
 a.db.close()

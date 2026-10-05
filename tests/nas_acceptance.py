"""Run only in an isolated acceptance container and its dedicated download mount."""
import sys,json,time,hashlib,urllib.request
from pathlib import Path
sys.path.insert(0,'/app')
from server import Vault,uid
c=json.loads(Path('/private/config.json').read_text());c.update(state='/test-state',storage='/test-data',host_storage='/home/nas3/download/.nova-vault-acceptance-20261002')
a=Vault(**c)
urls=['https://raw.githubusercontent.com/aria2/aria2/master/COPYING','https://raw.githubusercontent.com/mayswind/AriaNg/master/LICENSE']
created=[]
try:
 for url in urls:created.append(a.create_task({'url':url,'hidden':True,'request_key':uid()}))
 deadline=time.time()+90
 while time.time()<deadline:
  a.sync();s=a.snapshot()
  if len(s['files'])==2:break
  if any(t['status']=='error' for t in s['tasks']):raise RuntimeError(str(s['tasks']))
  time.sleep(2)
 fs=a.snapshot()['files'];assert len(fs)==2,'Downloads did not finish'
 for f in fs:
  t=next(t for t in created if t['id']==f['task_id']);source=urllib.request.urlopen(t['url'],timeout=15).read()
  assert hashlib.sha256(source).digest()==hashlib.sha256(a.path(f['zone'],f['rel']).read_bytes()).digest()
 print('PASS: real aria downloads and SHA-256 verification',flush=True)
 a.favorite(fs[0]['id'],True)
 def op(action,files):
  p=a.preview({'action':action,'file_ids':[f['id'] for f in files]});a.commit(p['id']);a.run_ops()
  b=next(b for b in a.snapshot()['batches'] if b['id']==p['id']);assert b['status']=='done',str(b)
 op('expose',fs);s=a.snapshot();assert all(f['zone']=='temporary' for f in s['files']);assert sum(f['favorite'] for f in s['files'])==1
 print('PASS: publish files, preserve favorite',flush=True)
 # Recreate application object to prove actual persistence across process state.
 a.db.close();a=Vault(**c)
 op('hide',a.snapshot()['files']);assert all(f['zone']=='hidden' for f in a.snapshot()['files'])
 print('PASS: reload database and restore all files',flush=True)
 op('delete_unstarred',a.snapshot()['files']);remaining=a.snapshot()['files'];assert len(remaining)==1 and remaining[0]['favorite']
 print('PASS: delete non-favorites and protect favorite',flush=True)
 a.favorite(remaining[0]['id'],False);op('delete_unstarred',a.snapshot()['files']);assert not a.snapshot()['files']
 print('PASS: isolated test file cleanup',flush=True)
finally:
 for t in a.rows('SELECT gid FROM tasks'):
  try:a.rpc('removeDownloadResult',t['gid'])
  except Exception:pass
 a.db.close()

"""One-time, lossless repair of the selected October 2026 archived downloads.
Run with the web worker stopped. Never delete conflicting files.
"""
import json,time,stat
from pathlib import Path
from server import Vault,download_identity,move_no_replace
app=Vault(**json.load(open('/private/config.json')))
files=app.rows("SELECT * FROM files WHERE zone='temporary' AND state='ready' AND rel LIKE '2026-10%'")
assert len(files)==21,'Unexpected selection; inspect before repair'
tasks={f['task_id']:dict(app.db.execute('SELECT * FROM tasks WHERE id=?',(f['task_id'],)).fetchone()) for f in files}
for f in files:
    p=app.path('temporary',f['rel']);s=p.stat()
    assert stat.S_ISREG(s.st_mode) and (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)==(f['dev'],f['ino'],f['size'],f['mtime']),'Original changed'
roots={str(app.host_root/app.dirs[z]/(t['folder'] or t['date'])/t['id']):t for t in tasks.values() for z in app.dirs}
def related(j):
    t=roots.get(j.get('dir'))
    if not t:return False
    assert t['status']=='complete'
    if j.get('infoHash'):assert download_identity(t['url'])=='btih:'+j['infoHash'].lower(),'Different torrent in managed directory'
    else:assert j.get('files') and all(Path(f['path']).name.startswith('[METADATA]') for f in j['files']),'Unknown job in directory'
    return True
app.acceleration_apply(app.acceleration_status())
removed=[]
for attempt in range(10):
    matches=[j for j in app.maintenance_jobs() if related(j)]
    if not matches:break
    for j in matches:
        app.rpc('changeOption',j['gid'],{'force-save':'false'})
        app.rpc('forceRemove',j['gid']);removed.append(j['gid'])
    time.sleep(.5)
assert not any(related(j) for j in app.maintenance_jobs()),'Duplicate still active'
# Remove only this completed selection's history from Aria's restart session;
# the application keeps its complete history in SQLite.
for j in app.rpc('tellStopped',0,1000,['gid','dir','files','infoHash']):
    if related(j):app.rpc('removeDownloadResult',j['gid'])
app.rpc('saveSession')
recovery=app.root/'.recovery'/('replayed-'+time.strftime('%Y%m%d-%H%M%S'))
recovery.mkdir(parents=True,exist_ok=False)
manifest={'removed_jobs':removed,'preserved_conflicts':[],'file_count':len(files)}
for t in tasks.values():
    rel=(t['folder'] or t['date'])+'/'+t['id'];p=app.path('hidden',rel)
    if p.exists():
        q=recovery/t['id'];move_no_replace(p,q)
        manifest['preserved_conflicts'].append({'from':str(p),'to':str(q)})
(recovery/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
preview=app.preview({'action':'hide','file_ids':[f['id'] for f in files]})
app.commit(preview['id']);app.run_ops();app.rpc('saveSession')
result=app.rows('SELECT status,error FROM ops WHERE batch_id=?',(preview['id'],))
assert len(result)==21 and all(o['status']=='done' for o in result),result
for f in files:
    assert app.path('hidden',f['rel']).exists() and not app.path('temporary',f['rel']).exists()
print(json.dumps({'moved':21,'removed_duplicate_jobs':len(removed),'preserved_conflict_folders':len(manifest['preserved_conflicts']),'recovery':str(recovery),'batch':preview['id']}))

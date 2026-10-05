"""Run inside the vault container after upgrade; imports deleted legacy audit entries only."""
import json,sqlite3,hashlib,datetime,os
with open('/tmp/nova-legacy-audit.json') as f:rows=json.load(f)['rows']
db=sqlite3.connect('/state/vault.db',timeout=60);db.row_factory=sqlite3.Row
count=0
for r in rows:
 original='legacy-'+hashlib.sha256(r['path'].encode()).hexdigest()[:24]
 if db.execute('SELECT 1 FROM files WHERE id=?',(original,)).fetchone():continue
 ident='legacy-history-'+hashlib.sha256(r['path'].encode()).hexdigest()[:24]
 if db.execute('SELECT 1 FROM files WHERE id=?',(ident,)).fetchone():continue
 created=datetime.datetime.fromtimestamp(r['created'],datetime.timezone.utc)
 date=(created+datetime.timedelta(hours=8)).date().isoformat();name=os.path.basename(r['path'])
 rel='history/'+r['month']+'/'+ident+'/'+name
 # Old audit has no per-file deletion timestamp; leave it unknown.
 deleted_at=''
 with db:
  db.execute('INSERT INTO tasks(id,url,zone,date,created_at,status,name,total,completed,folder,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,'','hidden',date,created.isoformat(),'complete',name,r['size'],r['size'],r['month'],'import'))
  db.execute('INSERT INTO files(id,task_id,rel,zone,size,dev,ino,mtime,state,deleted_at,deletion_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,ident,rel,'hidden',r['size'],r['dev'],r['ino'],r['mtime'],'deleted',deleted_at,'duplicate' if r['video'] else 'legacy_cleanup'))
 count+=1
print(json.dumps({'backfilled_deleted_records':count}))

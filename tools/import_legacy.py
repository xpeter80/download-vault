import os,sys
if '--background' in sys.argv:
 if os.fork():sys.exit(0)
 os.setsid()
 logfd=os.open('/var/lib/nova-download-vault/state/legacy-import.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
 os.dup2(logfd,1);os.dup2(logfd,2)
import os,json,sqlite3,hashlib,datetime,time
SOURCE='/home/nas3/download/.vr';ROOT='/home/nas3/download/nova-vault';STATE='/var/lib/nova-download-vault/state'
progress=STATE+'/legacy-import-progress.json';journal=STATE+'/legacy-import-journal.jsonl'
def report(**kw):
 data=dict(status='running',updated_at=datetime.datetime.utcnow().isoformat()+'Z');data.update(kw)
 tmp=progress+'.tmp'
 with open(tmp,'w') as f:json.dump(data,f)
 os.chmod(tmp,0o600);os.replace(tmp,progress)
def digest(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  while True:
   b=f.read(8*1024*1024)
   if not b:break
   h.update(b)
 return h.hexdigest()
def same_stat(p,r):
 s=os.stat(p,follow_symlinks=False)
 if (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)!=(r['dev'],r['ino'],r['size'],r['mtime']):raise RuntimeError('Source changed: '+p)
 return s
rows=[r for r in json.load(open('/tmp/nova-legacy-audit.json'))['rows'] if r['video']]
rows.sort(key=lambda r:(r['created'],r['path']))
db=sqlite3.connect(STATE+'/vault.db',timeout=60);db.row_factory=sqlite3.Row
import subprocess
subprocess.check_call(['docker','exec','nova-download-vault','python','-c',"import sqlite3; a=sqlite3.connect('/state/vault.db'); b=sqlite3.connect('/state/vault-before-legacy-import.db'); a.backup(b); b.close()"])
for name,ddl in [('folder',"TEXT DEFAULT ''"),('source',"TEXT DEFAULT 'download'")]:
 if name not in {r[1] for r in db.execute('PRAGMA table_info(tasks)')}:db.execute('ALTER TABLE tasks ADD COLUMN '+name+' '+ddl)
db.commit();dirs=json.loads(db.execute("SELECT value FROM settings WHERE key='dirs'").fetchone()[0])
known={}; sizes={}
for r in rows:sizes[r['size']]=sizes.get(r['size'],0)+1
for f in db.execute("SELECT * FROM files WHERE state='ready'"):
 p=os.path.join(ROOT,dirs[f['zone']],f['rel'])
 if f['size'] in sizes and os.path.isfile(p):known.setdefault(f['size'],[]).append((p,None))
count=dup=bytes_saved=0
fd=os.open(journal,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
with os.fdopen(fd,'a') as log:
 for i,r in enumerate(rows):
  src=os.path.join(SOURCE,r['path']);ident='legacy-'+hashlib.sha256(r['path'].encode()).hexdigest()[:24]
  if db.execute('SELECT 1 FROM files WHERE id=?',(ident,)).fetchone():continue
  same_stat(src,r);report(processed=i,total=len(rows),imported=count,duplicates=dup,phase='核验视频内容')
  candidates=known.setdefault(r['size'],[]);h=digest(src) if sizes[r['size']]>1 or candidates else None;canonical=None
  for k,(p,kh) in enumerate(candidates):
   if kh is None:kh=digest(p);candidates[k]=(p,kh)
   if kh==h:canonical=p;break
  same_stat(src,r)
  if canonical:
   if digest(canonical)!=h:raise RuntimeError('Canonical changed')
   log.write(json.dumps({'action':'duplicate','src':src,'canonical':canonical,'sha256':h})+'\n');log.flush();os.fsync(log.fileno())
   os.unlink(src);dup+=1;bytes_saved+=r['size'];continue
  month=r['month'];name=os.path.basename(src);rel=month+'/'+ident+'-'+name;dst=os.path.join(ROOT,dirs['hidden'],rel)
  os.makedirs(os.path.dirname(dst),exist_ok=True)
  log.write(json.dumps({'action':'import','src':src,'dst':dst,'id':ident})+'\n');log.flush();os.fsync(log.fileno())
  if not os.path.exists(dst):os.link(src,dst)
  elif os.stat(dst).st_ino!=os.stat(src).st_ino:raise RuntimeError('Target collision')
  s=os.stat(dst);dt=datetime.datetime.utcfromtimestamp(r['created'])+datetime.timedelta(hours=8)
  with db:
   db.execute('INSERT INTO tasks(id,url,zone,date,created_at,status,name,total,completed,folder,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,'','hidden',dt.date().isoformat(),datetime.datetime.utcfromtimestamp(r['created']).isoformat()+'Z','complete',name,s.st_size,s.st_size,month,'import'))
   db.execute('INSERT INTO files(id,task_id,rel,zone,size,dev,ino,mtime) VALUES(?,?,?,?,?,?,?,?)',(ident,ident,rel,'hidden',s.st_size,s.st_dev,s.st_ino,s.st_mtime_ns))
  same_stat(src,r);os.unlink(src);candidates.append((dst,h));count+=1
 report(processed=len(rows),total=len(rows),imported=count,duplicates=dup,bytes_saved=bytes_saved,phase='完成',status='complete')

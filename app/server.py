"""NAS download vault. Python stdlib only; a single durable worker owns file mutations."""
import contextlib, ctypes, datetime as dt, hashlib, hmac, html, http.server, ipaddress, json, os
import re, unicodedata, secrets, shutil, socket, sqlite3, stat, threading, time, urllib.parse, urllib.request
from pathlib import Path
from explorer import Explorer, download_identity
from discovery import Discovery
from maintenance import Maintenance

BASE=Path(__file__).parent
ZONE_NAMES={'hidden':'保险箱','visible':'普通可见','temporary':'临时可见'}
class AuthRedirect(Exception): pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl): return None

class Problem(Exception):
    def __init__(self,message,status=400,details=None): self.message,self.status,self.details=message,status,details or {}

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def dumps(v): return json.dumps(v,ensure_ascii=False,separators=(',',':'))
def uid(): return secrets.token_hex(12)

def safe_path(root,rel):
    root=Path(root).absolute(); p=root
    parts=Path(rel).parts
    if not parts or Path(rel).is_absolute() or any(x in ('..','.') for x in parts): raise Problem('非法文件路径')
    for part in parts:
        p=p/part
        if p.is_symlink(): raise Problem('不允许操作符号链接')
    if not p.resolve().is_relative_to(root.resolve()): raise Problem('文件超出管理目录')
    return p

def move_no_replace(src,dst):
    """Linux renameat2 guarantees never overwriting another file, including crash replay."""
    if os.name=='posix' and hasattr(ctypes.CDLL(None),'renameat2'):
        libc=ctypes.CDLL(None,use_errno=True)
        if libc.renameat2(-100,os.fsencode(src),-100,os.fsencode(dst),1):
            e=ctypes.get_errno(); raise OSError(e,os.strerror(e))
    else: # Local development fallback; production requires Linux renameat2.
        os.link(src,dst,follow_symlinks=False); os.unlink(src)
    for d in {src.parent,dst.parent}:
        fd=os.open(d,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)

from acceleration import Acceleration

class Vault(Maintenance,Acceleration):
    def __init__(self,state,storage,host_storage,rpc_url='',rpc_secret='',gateway_key='',origin='',rpc=None,auth_url=''):
        self.problem=Problem
        self.state=Path(state);self.root=Path(storage);self.host_root=Path(host_storage)
        self.state.mkdir(parents=True,exist_ok=True);self.root.mkdir(parents=True,exist_ok=True)
        self.acceleration_lock=threading.RLock();self.lock=threading.RLock();self.stop=threading.Event();self.rpc_url=rpc_url;self.rpc_secret=rpc_secret
        self.gateway_key=gateway_key;self.origin=origin;self.rpc_override=rpc;self.last_rpc_error=None
        self.auth_url=auth_url;self.auth_cache={};self.auth_lock=threading.Lock()
        self.db=sqlite3.connect(self.state/'vault.db',check_same_thread=False,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,gid TEXT UNIQUE,url TEXT,zone TEXT,date TEXT,created_at TEXT,status TEXT,name TEXT DEFAULT '',error TEXT DEFAULT '',total INTEGER DEFAULT 0,completed INTEGER DEFAULT 0,speed INTEGER DEFAULT 0,request_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,task_id TEXT REFERENCES tasks(id),rel TEXT UNIQUE,zone TEXT,favorite INTEGER DEFAULT 0,size INTEGER,dev INTEGER,ino INTEGER,mtime INTEGER,version INTEGER DEFAULT 0,state TEXT DEFAULT 'ready');
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,action TEXT,status TEXT,created_at TEXT,expires REAL,items TEXT);
CREATE TABLE IF NOT EXISTS ops(id TEXT PRIMARY KEY,batch_id TEXT REFERENCES batches(id),file_id TEXT,action TEXT,src TEXT,dst TEXT,status TEXT,error TEXT DEFAULT '',dev INTEGER,ino INTEGER);
''')
        for name,ddl in [('folder',"TEXT DEFAULT ''"),('source',"TEXT DEFAULT 'download'")]:
            if name not in {r[1] for r in self.db.execute('PRAGMA table_info(tasks)')}:self.db.execute('ALTER TABLE tasks ADD COLUMN '+name+' '+ddl)
        if 'media_status' not in {r[1] for r in self.db.execute('PRAGMA table_info(files)')}:self.db.execute("ALTER TABLE files ADD COLUMN media_status TEXT DEFAULT ''")
        if not self.db.execute('SELECT 1 FROM settings WHERE key=?',('dirs',)).fetchone():
            self.db.execute('INSERT INTO settings VALUES(?,?)',('dirs',dumps({'hidden':'.vault','visible':'visible','temporary':'temporary'})))
        for table,columns in [('tasks',[('tags',"TEXT DEFAULT '[]'")]),('files',[('tags',"TEXT DEFAULT '[]'"),('deleted_at',"TEXT DEFAULT ''"),('deletion_reason',"TEXT DEFAULT ''")])]:
            existing={r[1] for r in self.db.execute('PRAGMA table_info('+table+')')}
            for name,ddl in columns:
                if name not in existing:self.db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+ddl)
        self.db.execute('CREATE TABLE IF NOT EXISTS tags(name_key TEXT PRIMARY KEY,name TEXT NOT NULL)')
        self.db.execute("UPDATE files SET deleted_at=COALESCE((SELECT batches.created_at FROM ops JOIN batches ON batches.id=ops.batch_id WHERE ops.file_id=files.id AND ops.action='delete_unstarred' AND ops.status='done' ORDER BY batches.created_at DESC LIMIT 1),''),deletion_reason='cleanup' WHERE state='deleted' AND deleted_at='' AND deletion_reason=''")
        self.dirs=json.loads(self.db.execute('SELECT value FROM settings WHERE key="dirs"').fetchone()[0])
        for n in self.dirs.values(): (self.root/n).mkdir(exist_ok=True)
        if len({(self.root/n).stat().st_dev for n in self.dirs.values()})!=1: raise RuntimeError('三个目录必须位于同一文件系统')
        self.db.execute("UPDATE batches SET status='queued' WHERE status='running'")
        self.explorer=Explorer(self)
        self.discovery=Discovery(self)
    def rows(self,q,p=()):return [dict(r) for r in self.db.execute(q,p)]
    def path(self,zone,rel):return safe_path(self.root,self.dirs[zone]+'/'+rel)
    def rpc(self,method,*params):
        if self.rpc_override:return self.rpc_override(method,*params)
        p=(['token:'+self.rpc_secret] if self.rpc_secret else [])+list(params)
        req=urllib.request.Request(self.rpc_url,data=dumps({'jsonrpc':'2.0','id':uid(),'method':'aria2.'+method,'params':p}).encode(),headers={'Content-Type':'application/json'})
        try:
            # RPC is an explicitly configured local service, never a user-provided URL.
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req,timeout=12) as response:r=json.load(response)
            if 'error' in r:raise Problem('Aria2 拒绝操作，请检查任务状态或服务配置',502)
            return r['result']
        except Problem:raise
        except Exception:raise Problem('Aria2 暂时无法连接，请稍后重试',503)
    def validate_url(self,url):
        if len(url)>8192:raise Problem('链接过长')
        p=urllib.parse.urlsplit(url)
        if p.scheme=='magnet':
            if not any(x.startswith('urn:btih:') for x in urllib.parse.parse_qs(p.query).get('xt',[])):raise Problem('磁力链接缺少有效 btih')
            return
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:raise Problem('请输入 HTTP(S) 直链或磁力链接，不支持链接内账号密码')
        try:
            ips={a[4][0] for a in socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)}
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):raise Problem('下载链接必须指向公网地址')
        except socket.gaierror:raise Problem('无法解析下载地址')
    def normalize_tags(self,values):
        if not isinstance(values,list) or len(values)>20:raise Problem('最多选择 20 个标签')
        tags=[]
        for value in values:
            if not isinstance(value,str):raise Problem('标签格式无效')
            name=unicodedata.normalize('NFKC',value).strip()
            if not name or len(name)>40 or any(ord(c)<32 for c in name):raise Problem('标签需为 1–40 个字符')
            key=name.casefold();old=self.db.execute('SELECT name FROM tags WHERE name_key=?',(key,)).fetchone()
            name=old['name'] if old else name
            if not any(t.casefold()==key for t in tags):tags.append(name)
        return tags
    def save_tag_names(self,tags):
        for name in tags:self.db.execute('INSERT OR IGNORE INTO tags(name_key,name) VALUES(?,?)',(name.casefold(),name))
    def set_file_tags(self,ident,values):
        with self.lock:
            f=self.db.execute('SELECT id FROM files WHERE id=?',(ident,)).fetchone()
            if not f:raise Problem('文件记录不存在',404)
            tags=self.normalize_tags(values);self.save_tag_names(tags)
            self.db.execute('UPDATE files SET tags=? WHERE id=?',(dumps(tags),ident))
            return {'tags':tags}
    def bulk_file_update(self,d):
        ids=d.get('file_ids',[]);action=d.get('action','tags')
        if not isinstance(ids,list) or not ids or len(ids)>10000 or not all(isinstance(x,str) for x in ids):raise Problem('请选择文件，每批最多 10000 个')
        if action not in ('tags','favorite','unfavorite'):raise Problem('未知批量操作')
        tags=self.normalize_tags(d.get('tags',[])) if action=='tags' else []
        if action=='tags' and not tags:raise Problem('请先选择要添加的标签')
        with self.lock:
            files=[]
            for ident in set(ids):
                f=self.db.execute('SELECT * FROM files WHERE id=?',(ident,)).fetchone()
                if not f or f['state']!='ready':raise Problem('文件状态已变化，请重新选择',409)
                if self.db.execute("SELECT 1 FROM ops WHERE file_id=? AND status IN ('queued','intent')",(ident,)).fetchone():raise Problem('文件正在操作，请稍后重试',409)
                files.append(f)
            # Validate every merged tag set before writing anything.
            merged={f['id']:self.normalize_tags(json.loads(f['tags'] or '[]')+tags) for f in files} if action=='tags' else {}
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if action=='tags':
                    self.save_tag_names(tags)
                    for f in files:self.db.execute('UPDATE files SET tags=? WHERE id=?',(dumps(merged[f['id']]),f['id']))
                else:
                    for f in files:self.db.execute('UPDATE files SET favorite=?,version=version+1 WHERE id=?',(int(action=='favorite'),f['id']))
                self.db.execute('COMMIT')
            except Exception:self.db.execute('ROLLBACK');raise
            return {'count':len(files)}

    @staticmethod
    def resource_name(url):
        p=urllib.parse.urlsplit(url or '')
        if p.scheme=='magnet':return urllib.parse.parse_qs(p.query).get('dn',[''])[0]
        return urllib.parse.unquote(p.path.rsplit('/',1)[-1])
    @staticmethod
    def name_key(name):
        name=re.sub(r'^legacy-[0-9a-f]{24}-','',name or '')
        return unicodedata.normalize('NFKC',name).casefold().strip()
    def previous_downloads(self,url,name=''):
        identity=download_identity(url);target=self.name_key(name or self.resource_name(url));parsed=urllib.parse.urlsplit(url)
        with self.lock:
            matches=[]
            for t in self.rows('SELECT id,url,name,status,date,created_at,source FROM tasks ORDER BY created_at DESC'):
                fs=self.rows('SELECT rel,zone,state,deleted_at FROM files WHERE task_id=?',(t['id'],))
                reason=''
                if t['url'] and download_identity(t['url'])==identity:reason='来源链接或磁力内容标识相同'
                elif t['url'] and parsed.scheme in ('http','https'):
                    old=urllib.parse.urlsplit(t['url'])
                    if (old.hostname,old.path)==(parsed.hostname,parsed.path) and parsed.path not in ('','/') and old.query!=parsed.query:reason='同一资源路径，链接参数不同（可能相关）'
                names=[t['name']]+[Path(f['rel']).name for f in fs]
                if not reason and len(target)>=8 and any(self.name_key(n)==target for n in names):reason='文件名相同（可能相关，内容尚未验证）'
                if not reason:continue
                remaining=[f for f in fs if f['state']!='deleted'];deleted=[f for f in fs if f['state']=='deleted']
                t.pop('url');t.update(remaining_files=len(remaining),deleted_files=len(deleted),zones=list(dict.fromkeys(f['zone'] for f in remaining)),match_reason=reason,deleted_at=max((f['deleted_at'] or '' for f in deleted),default=''))
                matches.append(t)
                if len(matches)==10:break
            return matches
    def check_download_history(self,url,allow_duplicate=False):
        previous=self.previous_downloads(url)
        if previous and allow_duplicate is not True:
            raise Problem('该资源已有下载或删除记录，请确认是否再次下载',409,{'code':'DUPLICATE_DOWNLOAD','previous':previous})
        return previous
    def file_history(self,query='',offset=0):
        query=str(query).strip()[:200]
        try:offset=max(0,int(offset))
        except (ValueError,TypeError):raise Problem('历史页码无效')
        with self.lock:
            rows=self.rows('SELECT files.*,tasks.url,tasks.source,tasks.date,tasks.created_at FROM files JOIN tasks ON tasks.id=files.task_id ORDER BY tasks.created_at DESC,files.id')
            for f in rows:f['name']=Path(f['rel']).name;f['tags']=json.loads(f['tags'] or '[]')
            rows=[f for f in rows if query.casefold() in (f['name']+' '+(f['url'] or '')+' '+' '.join(f['tags'])).casefold()]
            return {'files':rows[offset:offset+50],'total':len(rows),'offset':offset}
    def create_task(self,data):
        url=str(data.get('url','')).strip();key=str(data.get('request_key',''))
        if not 12<=len(key)<=100:raise Problem('缺少请求标识，请刷新重试')
        with self.lock:
            old=self.db.execute('SELECT * FROM tasks WHERE request_key=?',(key,)).fetchone()
            if old:return dict(old)
        self.validate_url(url)
        with self.lock:
            old=self.db.execute('SELECT * FROM tasks WHERE request_key=?',(key,)).fetchone()
            if old:return dict(old)
            tags=self.normalize_tags(data.get('tags',[]))
            self.check_download_history(url,data.get('allow_duplicate',False))
            ident=uid();gid=secrets.token_hex(8);zone='hidden' if data.get('hidden',True) else 'visible'
            date=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
            folder=date[:7];rel=folder+'/'+ident;self.path(zone,rel).mkdir(parents=True)
            # Intent is durable before RPC; a fixed GID reconciles a lost response.
            self.db.execute('INSERT INTO tasks(id,gid,url,zone,date,created_at,status,request_key,folder) VALUES(?,?,?,?,?,?,?,?,?)',(ident,gid,url,zone,date,now(),'submitting',key,folder))
            self.save_tag_names(tags);self.db.execute('UPDATE tasks SET tags=? WHERE id=?',(dumps(tags),ident))
            self.submit(dict(self.db.execute('SELECT * FROM tasks WHERE id=?',(ident,)).fetchone()))
            return dict(self.db.execute('SELECT * FROM tasks WHERE id=?',(ident,)).fetchone())
    def restored_job(self,t,jobs=None):
        # Session restore may allocate a new GID. Only exact managed task dirs match.
        root=str(self.host_root/self.dirs[t['zone']]/(t.get('folder') or t['date'])/t['id'])
        matches=[j for j in (jobs if jobs is not None else self.maintenance_jobs()) if j.get('dir')==root]
        payload=[j for j in matches if not self.metadata_only(j)]
        candidates=payload or matches
        if len(candidates)==1:return candidates[0]
        return None

    def submit(self,t):
        try:
            try:r=self.rpc('tellStatus',t['gid']);self.db.execute('UPDATE tasks SET status=? WHERE id=?',(r['status'],t['id']));return
            except Problem:
                restored=self.restored_job(t)
                if restored:
                    r=self.rpc('tellStatus',restored['gid'])
                    self.db.execute('UPDATE tasks SET gid=?,status=?,error=? WHERE id=?',(restored['gid'],r['status'],'',t['id']));return
            options={'gid':t['gid'],'dir':str(self.host_root/self.dirs[t['zone']]/(t.get('folder') or t['date'])/t['id']),'force-save':'false','allow-overwrite':'false','auto-file-renaming':'true','seed-time':self.acceleration_status()['options']['seed-time'],'check-certificate':'true'}
            self.rpc('addUri',[t['url']],options)
            self.db.execute("UPDATE tasks SET status='waiting',error='' WHERE id=?",(t['id'],))
        except Problem as e:self.db.execute("UPDATE tasks SET status='submitting',error=? WHERE id=?",(e.message,t['id']))
    def sync(self):
        with self.lock:
            try:self.rpc('getVersion');self.last_rpc_error=None
            except Problem as e:self.last_rpc_error=e.message;return
            restored_jobs=None
            for t in self.rows("SELECT * FROM tasks WHERE status NOT IN ('complete','removed')"):
                if t['status']=='submitting':self.submit(t);continue
                try:
                    try:r=self.rpc('tellStatus',t['gid'])
                    except Problem:
                        if restored_jobs is None:restored_jobs=self.maintenance_jobs()
                        restored=self.restored_job(t,restored_jobs)
                        if not restored:raise
                        r=self.rpc('tellStatus',restored['gid'])
                        self.db.execute('UPDATE tasks SET gid=? WHERE id=?',(restored['gid'],t['id']))
                    followed=r.get('followedBy',[])
                    if followed:
                        self.db.execute('UPDATE tasks SET gid=? WHERE id=?',(followed[0],t['id']));r=self.rpc('tellStatus',followed[0])
                    name=r.get('bittorrent',{}).get('info',{}).get('name','')
                    if not name and r.get('files'):
                        name=Path(r['files'][0].get('path','')).name.removeprefix('[METADATA]')
                    if name and not t['name']:
                        self.db.execute('UPDATE tasks SET name=? WHERE id=?',(name,t['id']));t['name']=name
                    state=r['status'];error=''
                    if state=='error':error='下载失败（Aria2 错误码 '+str(r.get('errorCode','?'))+'），可重试；网页链接请换用可下载的文件直链'
                    self.db.execute('UPDATE tasks SET status=?,total=?,completed=?,speed=?,error=? WHERE id=?',(state,int(r.get('totalLength',0)),int(r.get('completedLength',0)),int(r.get('downloadSpeed',0)),error,t['id']))
                    if state=='complete':self.index_files(t,r)
                except Problem as e:self.db.execute('UPDATE tasks SET error=? WHERE id=?',(e.message,t['id']))
            for f in self.rows("SELECT * FROM files WHERE state IN ('ready','missing')"):
                try:
                    s=self.path(f['zone'],f['rel']).stat()
                    valid=stat.S_ISREG(s.st_mode) and (s.st_dev,s.st_ino)==(f['dev'],f['ino']) and s.st_size==f['size'] and s.st_mtime_ns==f['mtime']
                except (OSError,Problem):valid=False
                self.db.execute('UPDATE files SET state=? WHERE id=?',('ready' if valid else 'missing',f['id']))
    def index_files(self,t,r):
        expected=self.host_root/self.dirs[t['zone']]/(t.get('folder') or t['date'])/t['id']
        for f in r.get('files',[]):
            try:
                hp=Path(f['path']);inner=hp.relative_to(expected);rel=str(Path(t.get('folder') or t['date'])/t['id']/inner)
                p=self.path(t['zone'],rel);s=p.stat()
                if not stat.S_ISREG(s.st_mode) or int(f.get('completedLength',0))!=int(f.get('length',0)):continue
                self.db.execute('INSERT OR IGNORE INTO files(id,task_id,rel,zone,size,dev,ino,mtime) VALUES(?,?,?,?,?,?,?,?)',(uid(),t['id'],rel,t['zone'],s.st_size,s.st_dev,s.st_ino,s.st_mtime_ns))
                self.db.execute("UPDATE files SET tags=? WHERE task_id=? AND rel=?",(t.get('tags') or '[]',t['id'],rel))
                if not t['name']:self.db.execute('UPDATE tasks SET name=? WHERE id=?',(p.name,t['id']))
            except (ValueError,OSError,Problem):continue
    def assert_idle(self,task_id):
        t=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not t or t['status']!='complete':raise Problem('任务尚未完成，已跳过')
        self.clear_completed_metadata(task_id)
        self.rpc('getVersion') # Fail closed when activity cannot be checked.
        # Completed GID may have expired from aria history; active & waiting dirs remain authoritative.
        jobs=self.rpc('tellActive',['gid','dir','files'])
        offset=0
        while True:
            pending=self.rpc('tellWaiting',offset,1000,['gid','dir','files']);jobs+=pending
            if len(pending)<1000:break
            offset+=len(pending)
            if offset>=10000:raise Problem('任务过多，暂时无法确认文件空闲')
        roots=[str(self.host_root/n/(t['folder'] or t['date'])/t['id']) for n in self.dirs.values()]
        roots += [str(self.host_root/self.dirs[f['zone']]/f['rel']) for f in self.rows('SELECT zone,rel FROM files WHERE task_id=?',(task_id,))]
        for j in jobs:
            paths=[str(j.get('dir',''))]+[str(x.get('path','')) for x in j.get('files',[])]
            if j.get('gid')==t['gid'] or any(p==r or p.startswith(r+'/') for p in paths for r in roots):raise Problem('Aria2 仍在使用该文件，已跳过')
    def control(self,ident,action):
        with self.lock:
            t=self.db.execute('SELECT * FROM tasks WHERE id=?',(ident,)).fetchone()
            if not t:raise Problem('任务不存在',404)
            if action=='pause' and t['status'] in ('active','waiting'):self.rpc('pause',t['gid'])
            elif action=='resume' and t['status']=='paused':self.rpc('unpause',t['gid'])
            elif action=='retry' and t['status'] in ('error','submitting','removed'):
                if self.db.execute('SELECT 1 FROM files WHERE task_id=?',(ident,)).fetchone():raise Problem('已有管理文件，不可重新下载到原路径')
                if t['status']!='submitting':self.db.execute('UPDATE tasks SET gid=? WHERE id=?',(secrets.token_hex(8),ident))
                self.db.execute("UPDATE tasks SET status='submitting',error='' WHERE id=?",(ident,))
            else:raise Problem('当前状态不支持此操作')
    def favorite(self,ident,value):
        with self.lock:
            f=self.db.execute('SELECT * FROM files WHERE id=?',(ident,)).fetchone()
            if not f or f['state']=='deleted':raise Problem('文件不存在',404)
            if self.db.execute("SELECT 1 FROM ops WHERE file_id=? AND status IN ('queued','intent')",(ident,)).fetchone():raise Problem('文件正在操作，请稍后再收藏',409)
            self.db.execute('UPDATE files SET favorite=?,version=version+1 WHERE id=?',(int(bool(value)),ident))
    def preview(self,d):
        with self.lock:
            action=d.get('action')
            if action not in ('expose','hide','delete_unstarred'):raise Problem('未知操作')
            ids=d.get('file_ids',[])
            if not isinstance(ids,list) or len(ids)>10000:raise Problem('每批最多 10000 个文件')
            fs=[];skip=0
            for ident in set(ids):
                f=self.db.execute('SELECT * FROM files WHERE id=?',(ident,)).fetchone()
                if not f or f['state']!='ready' or (action=='expose' and f['zone']!='hidden') or (action=='hide' and f['zone']!='temporary') or (action=='delete_unstarred' and f['favorite']):skip+=1;continue
                fs.append({'id':f['id'],'version':f['version'],'name':Path(f['rel']).name,'size':f['size']})
            if not fs:raise Problem('没有可操作的文件；收藏、缺失或当前区域不匹配的文件已跳过')
            ident=uid();self.db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)',(ident,action,'preview',now(),time.time()+300,dumps(fs)))
            return {'id':ident,'action':action,'files':fs,'count':len(fs),'size':sum(f['size'] for f in fs),'skipped':skip}
    def commit(self,ident):
        with self.lock:
            b=self.db.execute('SELECT * FROM batches WHERE id=?',(ident,)).fetchone()
            if not b:raise Problem('操作不存在',404)
            if b['status']!='preview':return {'id':ident,'status':b['status']}
            if b['expires']<time.time():raise Problem('预览已过期，请重新选择',409)
            items=json.loads(b['items'])
            self.db.execute('BEGIN IMMEDIATE')
            try:
                for x in items:
                    f=self.db.execute('SELECT * FROM files WHERE id=?',(x['id'],)).fetchone()
                    if f['version']!=x['version'] or f['state']!='ready':raise Problem('文件状态有变化，请重新预览',409)
                    if self.db.execute("SELECT 1 FROM ops WHERE file_id=? AND status IN ('queued','intent')",(f['id'],)).fetchone():raise Problem('文件已有进行中操作',409)
                    dst='temporary' if b['action']=='expose' else 'hidden'
                    self.db.execute('INSERT INTO ops(id,batch_id,file_id,action,src,dst,status,dev,ino) VALUES(?,?,?,?,?,?,?,?,?)',(uid(),ident,f['id'],b['action'],f['zone'],dst,'queued',f['dev'],f['ino']))
                self.db.execute("UPDATE batches SET status='queued' WHERE id=?",(ident,));self.db.execute('COMMIT')
            except Exception:self.db.execute('ROLLBACK');raise
            return {'id':ident,'status':'queued'}
    def run_ops(self):
        with self.lock:
            for b in self.rows("SELECT * FROM batches WHERE status IN ('queued','running') ORDER BY created_at"):
                self.db.execute("UPDATE batches SET status='running' WHERE id=?",(b['id'],))
                for o in self.rows("SELECT * FROM ops WHERE batch_id=? AND status IN ('queued','intent')",(b['id'],)):
                    try:self.run_one(o)
                    except Exception as e:
                        message=e.message if isinstance(e,Problem) else ('目标文件已存在，未覆盖' if isinstance(e,FileExistsError) else '文件操作失败，请检查权限、空间或外部变更')
                        self.db.execute("UPDATE ops SET status='failed',error=? WHERE id=?",(message,o['id']))
                bad=self.db.execute("SELECT count(*) FROM ops WHERE batch_id=? AND status='failed'",(b['id'],)).fetchone()[0]
                self.db.execute('UPDATE batches SET status=? WHERE id=?',('partial' if bad else 'done',b['id']))
    def run_one(self,o):
        f=self.db.execute('SELECT * FROM files WHERE id=?',(o['file_id'],)).fetchone()
        src=self.path(o['src'],f['rel']);dst=self.path(o['dst'],f['rel'])
        # Durable intent lets a restart reconcile rename/unlink before DB acknowledgement.
        if o['status']=='intent' and not src.exists():
            if o['action']=='delete_unstarred':self.finish(o,f);return
            if dst.exists() and (dst.stat().st_dev,dst.stat().st_ino)==(o['dev'],o['ino']):self.finish(o,f);return
            raise Problem('源文件缺失且无法确认目标，需检查')
        if f['state']!='ready':raise Problem('文件状态异常，已跳过')
        if o['action']=='delete_unstarred' and f['favorite']:raise Problem('文件已收藏，已保护')
        self.assert_idle(f['task_id'])
        s=src.stat()
        if (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)!=(f['dev'],f['ino'],f['size'],f['mtime']):raise Problem('文件被外部修改，已跳过')
        self.db.execute("UPDATE ops SET status='intent',error='' WHERE id=?",(o['id'],))
        if o['action']=='delete_unstarred':
            src.unlink()
            fd=os.open(src.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        else:
            dst.parent.mkdir(parents=True,exist_ok=True)
            # Recheck path components after mkdir before the non-overwriting rename.
            self.path(o['dst'],f['rel']);move_no_replace(src,dst)
        self.finish(o,f)
        p=src.parent;root=self.root/self.dirs[o['src']]
        while p!=root and p.is_relative_to(root):
            try:p.rmdir()
            except OSError:break
            p=p.parent
    def finish(self,o,f):
        if o['action']=='delete_unstarred':self.db.execute("UPDATE files SET state='deleted',deleted_at=?,deletion_reason='cleanup',version=version+1 WHERE id=?",(now(),f['id']))
        else:self.db.execute("UPDATE files SET zone=?,version=version+1,state='ready' WHERE id=?",(o['dst'],f['id']))
        self.db.execute("UPDATE ops SET status='done',error='' WHERE id=?",(o['id'],))
    def retry_batch(self,ident):
        with self.lock:
            rows=self.rows("SELECT * FROM ops WHERE batch_id=? AND status='failed'",(ident,))
            if not rows:raise Problem('没有失败项可重试')
            # Preserve intent when filesystem may already have changed.
            for o in rows:self.db.execute("UPDATE ops SET status='intent' WHERE id=?",(o['id'],))
            self.db.execute("UPDATE batches SET status='queued' WHERE id=?",(ident,))
    def settings(self,d):
        names={k:str(d.get(k,'')) for k in self.dirs}
        if len(set(names.values()))!=3 or any(not n or n in ('.','..') or '/' in n or '\\' in n or len(n)>80 or any(ord(c)<32 for c in n) for n in names.values()) or not names['hidden'].startswith('.') or any(names[k].startswith('.') for k in ('visible','temporary')):raise Problem('目录名称不合法或重复；仅隐藏目录应以点开头')
        with self.lock:
            if names==self.dirs:return
            if self.db.execute('SELECT 1 FROM tasks LIMIT 1').fetchone():raise Problem('已有下载记录，目录更改需要迁移；请联系管理员处理，避免丢失文件位置',409)
            for n in names.values():
                p=safe_path(self.root,n)
                if p.exists() and any(p.iterdir()):raise Problem('新目录必须为空')
                p.mkdir(exist_ok=True)
            if len({(self.root/n).stat().st_dev for n in names.values()})!=1:raise Problem('目录不在同一文件系统')
            self.db.execute('UPDATE settings SET value=? WHERE key="dirs"',(dumps(names),));self.dirs=names
    def snapshot(self):
        with self.lock:
            fs=self.rows("SELECT files.*,tasks.date,tasks.created_at FROM files JOIN tasks ON tasks.id=files.task_id WHERE state!='deleted' ORDER BY tasks.date DESC,rel")
            for f in fs:f['name']=Path(f['rel']).name;f['tags']=json.loads(f['tags'] or '[]')
            batches=self.rows("SELECT id,action,status,created_at FROM batches WHERE status!='preview' ORDER BY created_at DESC LIMIT 30")
            for b in batches:
                b['operations']=self.rows('SELECT ops.status,ops.error,files.rel FROM ops LEFT JOIN files ON files.id=ops.file_id WHERE batch_id=?',(b['id'],))
            usage=shutil.disk_usage(self.root)
            tasks=self.rows("SELECT id,name,zone,date,status,error,total,completed,speed,tags FROM tasks WHERE source!='import' ORDER BY created_at DESC LIMIT 1000")
            for t in tasks:t['tags']=json.loads(t['tags'] or '[]')
            return {'files':fs,'tasks':tasks,'tags':[r['name'] for r in self.rows('SELECT name FROM tags ORDER BY name_key')],'batches':batches,'settings':self.dirs,'rpc_error':self.last_rpc_error,'free':usage.free,'managed_size':sum(f['size'] for f in fs),'version':'1.9.2'}
    def worker(self):
        while not self.stop.is_set():
            try:self.discovery.recover();self.run_ops();self.sync()
            except Exception as e:print('worker error:',type(e).__name__,flush=True)
            self.stop.wait(3)

class Handler(http.server.BaseHTTPRequestHandler):
    server_version='NovaVault/1.0'
    def log_message(self,fmt,*args):pass # Never log signed download URLs or authorization headers.
    @property
    def app(self):return self.server.app
    def reply(self,status,data,ctype='application/json; charset=utf-8'):
        body=dumps(data).encode() if ctype.startswith('application/json') else data
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src https: http:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.end_headers();self.wfile.write(body)
    def authorize(self):
        if self.app.auth_url:
            cookie=self.headers.get('Cookie','')
            if not cookie:raise AuthRedirect()
            digest=hashlib.sha256(cookie.encode()).hexdigest()
            with self.app.auth_lock:valid=self.app.auth_cache.get(digest,0)>time.time()
            if not valid:
                req=urllib.request.Request(self.app.auth_url,headers={'Cookie':cookie,'X-Forwarded-Uri':'/downloads/','X-Forwarded-Method':'GET','X-Forwarded-Host':urllib.parse.urlsplit(self.app.origin).netloc})
                try:
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
                    with opener.open(req,timeout=10) as r:
                        if r.status!=200:raise Problem('当前账号没有保险箱访问权限',403)
                except urllib.error.HTTPError as e:
                    if e.code in (301,302,303,307,308,401):raise AuthRedirect()
                    # The existing Nova bridge uses 403 for refresh failures as well
                    # as real authorization denial. Only known session errors re-login.
                    try:detail=json.loads(e.read(8192)).get('error','')
                    except (ValueError,AttributeError):detail=''
                    if e.code==403 and any(msg in str(detail) for msg in ('登录续期失败','登录已失效','登录已过期','会话已失效','会话已过期')):
                        raise AuthRedirect()
                    if e.code==403:raise Problem('当前 Nova 账号没有下载保险箱的访问权限，请切换到老板账号。',403)
                    raise Problem('Nova 登录服务暂时不可用，请稍后重试。',503)
                except Problem:raise
                except Exception:raise Problem('登录服务暂时不可用，请稍后重试',503)
                with self.app.auth_lock:
                    self.app.auth_cache={k:v for k,v in self.app.auth_cache.items() if v>time.time()}
                    self.app.auth_cache[digest]=time.time()+30
        else:
            key=self.headers.get('X-Vault-Gateway','')
            if not self.app.gateway_key or not hmac.compare_digest(key,self.app.gateway_key):raise Problem('请通过 NAS 的 HTTPS 入口登录',401)
        if self.command!='GET' and self.headers.get('Origin')!=self.app.origin:raise Problem('请求来源无效，请从正式入口操作',403)
    def dispatch(self):
        path=urllib.parse.urlsplit(self.path).path
        if self.command=='GET' and path=='/healthz':return self.reply(200,{'ok':True,'version':'1.9.2'})
        if self.command=='GET' and path=='/login':return self.login_redirect()
        # Public assets contain no user data and also style the sign-in/error page.
        if self.command=='GET' and path in ('/resource-types.js','/style.css','/app.js','/explore.js','/discovery.js','/mobile.js'):
            mime='text/css; charset=utf-8' if path.endswith('.css') else 'text/javascript; charset=utf-8'
            return self.reply(200,(BASE/'static'/path[1:]).read_bytes(),mime)
        self.authorize()
        if self.command=='GET':
            if path=='/api/file-history':
                q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                return self.reply(200,self.app.file_history(q.get('q',[''])[0],q.get('offset',[0])[0]))
            if path=='/api/acceleration':return self.reply(200,self.app.acceleration_view())
            if path=='/api/state':return self.reply(200,self.app.snapshot())
            if path=='/api/discovery/history':return self.reply(200,{'jobs':self.app.discovery.history()})
            if path=='/api/discovery/job':
                ident=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('id',[''])[0]
                return self.reply(200,self.app.discovery.get(ident))
            if path=='/api/explore/history':return self.reply(200,{'history':self.app.explorer.history()})
            if path=='/api/explore/page':
                ident=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('id',[''])[0]
                return self.reply(200,self.app.explorer.saved(ident))
            static={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
            if path in static:
                f,mime=static[path];return self.reply(200,(BASE/'static'/f).read_bytes(),mime)
            raise Problem('页面不存在',404)
        length=int(self.headers.get('Content-Length','0'))
        if length>2000000:raise Problem('请求过大',413)
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise Problem('需要 JSON 请求',415)
        try:d=json.loads(self.rfile.read(length))
        except Exception:raise Problem('请求格式错误')
        if not isinstance(d,dict):raise Problem('请求格式错误')
        if path=='/api/discovery/start':r=self.app.discovery.start(d)
        elif path=='/api/discovery/cancel':r=self.app.discovery.cancel(d.get('id'))
        elif path=='/api/discovery/download':r=self.app.discovery.download(d)
        elif path=='/api/explore/visit':r=self.app.explorer.visit(d.get('url'))
        elif path=='/api/explore/download':r=self.app.explorer.download(d)
        elif path=='/api/explore/history/delete':self.app.explorer.remove(d.get('id'));r={'ok':True}
        elif path=='/api/tasks':r=self.app.create_task(d)
        elif path=='/api/tasks/control':self.app.control(d.get('id'),d.get('action'));r={'ok':True}
        elif path=='/api/file-tags/bulk':r=self.app.bulk_file_update(d)
        elif path=='/api/file-tags':r=self.app.set_file_tags(d.get('id'),d.get('tags',[]))
        elif path=='/api/favorite':self.app.favorite(d.get('id'),d.get('favorite'));r={'ok':True}
        elif path=='/api/preview':r=self.app.preview(d)
        elif path=='/api/commit':r=self.app.commit(d.get('id'))
        elif path=='/api/retry':self.app.retry_batch(d.get('id'));r={'ok':True}
        elif path=='/api/admin/cleanup/preview':r=self.app.maintenance_preview()
        elif path=='/api/admin/cleanup/commit':r=self.app.maintenance_commit(d.get('id'))
        elif path=='/api/admin/restart':
            action=d.get('action')
            if action not in ('downloader','nas') or d.get('confirmation')!='确认重启':raise Problem('请确认重启操作')
            directory=Path('/admin-control')
            if not directory.is_dir():raise Problem('NAS 管理功能尚未配置',503)
            if any(directory.glob('*.request')):raise Problem('已有重启操作等待执行',409)
            try:self.app.rpc('saveSession')
            except Exception:raise Problem('无法保存下载会话，请等待下载器恢复后重试',503)
            with self.app.lock:self.app.db.execute('PRAGMA wal_checkpoint(FULL)')
            target=directory/(action+'.request')
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(now());f.flush();os.fsync(f.fileno())
            r={'ok':True,'message':'重启已提交，未完成下载将在启动后继续'}
        elif path=='/api/acceleration':r=self.app.acceleration_save(d)
        elif path=='/api/acceleration/sync':r=self.app.acceleration_sync()
        elif path=='/api/settings':self.app.settings(d);r={'ok':True}
        else:raise Problem('接口不存在',404)
        self.reply(200,r)
    def login_redirect(self):
        # Always start OAuth with application context; /auth/login alone goes to /account.
        self.send_response(303);self.send_header('Location','/api/auth/start?return=%2Fdownloads%2F');self.send_header('Cache-Control','no-store');self.send_header('Content-Length','0');self.end_headers()
    def handle_request(self):
        try:self.dispatch()
        except AuthRedirect:
            if urllib.parse.urlsplit(self.path).path.startswith('/api/'):
                self.reply(401,{'error':'登录已过期，请重新登录后返回保险箱。','code':'LOGIN_REQUIRED','login_url':'/downloads/login'})
            else:self.login_redirect()
        except Problem as e:
            if self.command=='GET' and urllib.parse.urlsplit(self.path).path=='/':
                page=(BASE/'static'/'auth-error.html').read_text().replace('{{message}}',html.escape(e.message))
                self.reply(e.status,page.encode(),'text/html; charset=utf-8')
            else:self.reply(e.status,{'error':e.message,**e.details})
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as e:
            print('request error:',type(e).__name__,flush=True)
            with contextlib.suppress(Exception):self.reply(500,{'error':'服务处理失败，请稍后重试'})
    do_GET=handle_request
    do_POST=handle_request

if __name__=='__main__':
    config=json.loads(Path(os.environ.get('VAULT_CONFIG','/private/config.json')).read_text())
    app=Vault(**config)
    server=http.server.ThreadingHTTPServer(('0.0.0.0',8000),Handler);server.app=app
    threading.Thread(target=app.worker,daemon=True).start()
    threading.Thread(target=app.acceleration_worker,daemon=True).start()
    print('Nova download vault listening on 8000',flush=True)
    server.serve_forever()

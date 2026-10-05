"""Conservative cleanup of completed-task metadata jobs and Aria2 sidecars."""
import json, os, secrets, stat, time
from pathlib import Path

class Maintenance:
    def maintenance_jobs(self):
        fields=['gid','dir','files','status']
        jobs=self.rpc('tellActive',fields);offset=0
        while True:
            page=self.rpc('tellWaiting',offset,1000,fields);jobs+=page
            if len(page)<1000:return jobs
            offset+=len(page)
            if offset>=10000:raise self.problem('下载任务过多，暂时无法安全检查残留')

    def completed_task_roots(self):
        roots={}
        for t in self.rows("SELECT * FROM tasks WHERE status='complete'"):
            fs=self.rows("SELECT * FROM files WHERE task_id=? AND state!='deleted'",(t['id'],))
            if not fs:continue
            valid=True
            for f in fs:
                try:
                    s=self.path(f['zone'],f['rel']).stat()
                    valid=valid and f['state']=='ready' and stat.S_ISREG(s.st_mode) and (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)==(f['dev'],f['ino'],f['size'],f['mtime'])
                except (OSError,self.problem):valid=False
            if not valid:continue
            for zone,name in self.dirs.items():
                rel=str(Path(t.get('folder') or t['date'])/t['id'])
                roots[str(self.host_root/name/rel)]={'task_id':t['id'],'zone':zone,'rel':rel}
        return roots

    @staticmethod
    def metadata_only(j):
        fs=j.get('files',[])
        return bool(fs) and all(str(f.get('path','')).startswith('[METADATA]') for f in fs)

    def residual_inventory(self):
        jobs=self.maintenance_jobs();roots=self.completed_task_roots();metadata=[];sidecars=[]
        for j in jobs:
            root=roots.get(str(j.get('dir','')))
            if root and self.metadata_only(j):
                metadata.append({'kind':'metadata','gid':j['gid'],'dir':j['dir'],'task_id':root['task_id'],'name':str(j['files'][0]['path'])})
        removable={j['gid'] for j in metadata}
        for host,root in roots.items():
            # Any payload or paused/waiting download protects its entire task directory.
            if any(j['gid'] not in removable and (str(j.get('dir',''))==host or str(j.get('dir','')).startswith(host+'/') or any(str(f.get('path','')).startswith(host+'/') for f in j.get('files',[]))) for j in jobs):continue
            base=self.path(root['zone'],root['rel'])
            if not base.is_dir():continue
            for directory,dirs,files in os.walk(base,followlinks=False):
                dirs[:]=[d for d in dirs if not (Path(directory)/d).is_symlink()]
                for name in files:
                    if not name.endswith('.aria2'):continue
                    p=Path(directory)/name
                    if p.is_symlink():continue
                    s=p.stat()
                    if not stat.S_ISREG(s.st_mode):continue
                    sidecars.append({'kind':'sidecar','zone':root['zone'],'rel':str(p.relative_to(self.root/self.dirs[root['zone']])),'name':name,'size':s.st_size,'dev':s.st_dev,'ino':s.st_ino,'mtime':s.st_mtime_ns})
        return metadata+sidecars

    def clear_completed_metadata(self,task_id):
        # Run during expose/hide: never bypass genuine payload activity.
        for item in self.residual_inventory():
            if item['kind']=='metadata' and item['task_id']==task_id:
                self.rpc('forceRemove',item['gid']);self.rpc('saveSession')

    def maintenance_preview(self):
        with self.lock:
            items=self.residual_inventory();ident=secrets.token_hex(12)
            self.db.execute('CREATE TABLE IF NOT EXISTS maintenance_plans(id TEXT PRIMARY KEY,expires REAL,items TEXT,result TEXT)')
            self.db.execute('DELETE FROM maintenance_plans WHERE expires<? AND result IS NULL',(time.time(),))
            self.db.execute('INSERT INTO maintenance_plans VALUES(?,?,?,NULL)',(ident,time.time()+300,json.dumps(items,ensure_ascii=False)))
            return {'id':ident,'items':items,'count':len(items),'size':sum(x.get('size',0) for x in items)}

    def maintenance_commit(self,ident):
        with self.lock:
            self.db.execute('CREATE TABLE IF NOT EXISTS maintenance_plans(id TEXT PRIMARY KEY,expires REAL,items TEXT,result TEXT)')
            plan=self.db.execute('SELECT * FROM maintenance_plans WHERE id=?',(ident,)).fetchone()
            if not plan:raise self.problem('清理预览不存在，请重新检查',404)
            if plan['result']:return json.loads(plan['result'])
            if plan['expires']<time.time():raise self.problem('清理预览已过期，请重新检查',409)
            done=0;skipped=0;errors=[]
            for item in json.loads(plan['items']):
                try:
                    fresh=self.residual_inventory()
                    if item not in fresh:skipped+=1;continue
                    if item['kind']=='metadata':
                        self.rpc('forceRemove',item['gid']);self.rpc('saveSession')
                    else:
                        # Path components and exact identity rechecked immediately before unlink.
                        p=self.path(item['zone'],item['rel']);s=p.stat()
                        if (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)!=(item['dev'],item['ino'],item['size'],item['mtime']):skipped+=1;continue
                        p.unlink();fd=os.open(p.parent,os.O_RDONLY)
                        try:os.fsync(fd)
                        finally:os.close(fd)
                    done+=1
                except Exception:
                    errors.append({'name':item['name'],'error':'未清理成功，文件状态可能变化或下载器连接失败'})
                    if item['kind']=='metadata':
                        skipped+=len(json.loads(plan['items']))-done-skipped-len(errors)
                        break # Do not unlink sidecars after a session-save failure.
            result={'done':done,'skipped':skipped,'errors':errors}
            self.db.execute('UPDATE maintenance_plans SET result=? WHERE id=?',(json.dumps(result,ensure_ascii=False),ident))
            return result

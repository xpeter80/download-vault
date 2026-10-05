"""Durable download tuning and daily, built-in public tracker refresh."""
import json,time,urllib.request,urllib.parse,os
from functools import wraps
def serialized(fn):
    @wraps(fn)
    def call(self,*args,**kwargs):
        with self.acceleration_lock:return fn(self,*args,**kwargs)
    return call
from pathlib import Path
DEFAULTS={'max-concurrent-downloads':'5','max-overall-download-limit':'0','max-overall-upload-limit':'50K','seed-time':'0','disk-cache':'64M','bt-max-peers':'100','max-connection-per-server':'8','enable-dht':'true','enable-peer-exchange':'true'}
SOURCES=['https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt','https://raw.githubusercontent.com/XIU2/TrackersListCollection/master/best.txt']
def trackers(text):
    out=[]
    for line in text.splitlines():
        u=line.strip()
        try:p=urllib.parse.urlsplit(u)
        except ValueError:continue
        if p.scheme in ('http','https','udp') and p.hostname and not p.username and len(u)<2048 and u not in out:out.append(u)
    return out[:100]
class Acceleration:
    def acceleration_status(self):
        with self.lock:
            row=self.db.execute("SELECT value FROM settings WHERE key='acceleration'").fetchone()
            return json.loads(row[0]) if row else {'options':dict(DEFAULTS),'auto':True,'custom':'','trackers':[],'sources':SOURCES,'last_success':0,'last_attempt':0,'error':'','applied':False}
    def acceleration_view(self):
        s=self.acceleration_status()
        try:
            actual=self.rpc('getGlobalOption')
            if isinstance(actual,dict):
                pending=[]
                if actual.get('disk-cache')!=str(int(s['options']['disk-cache'][:-1])*1024*1024):pending.append('磁盘缓存')
                if actual.get('enable-dht')!=s['options']['enable-dht']:pending.append('DHT')
                s['restart_required']=pending
        except Exception:s['error']='下载器暂时无法连接，显示已保存的设置'
        return s
    def acceleration_store(self,s):
        with self.lock:self.db.execute("INSERT OR REPLACE INTO settings VALUES('acceleration',?)",(json.dumps(s),))
    @serialized
    def acceleration_apply(self,s):
        options=dict(s['options']);all_trackers=list(dict.fromkeys(s.get('trackers',[])+trackers(s.get('custom',''))))
        options['bt-tracker']=','.join(all_trackers)
        self.rpc('changeGlobalOption',options)
        # Global options affect new tasks; update supported options on existing tasks too.
        supported={k:v for k,v in options.items() if k not in ('disk-cache','enable-dht','max-concurrent-downloads','max-overall-download-limit','max-overall-upload-limit')}
        failures=0
        for j in self.maintenance_jobs():
            opts={k:v for k,v in supported.items() if k!='bt-tracker'}
            if j.get('bittorrent'):opts['bt-tracker']=options['bt-tracker']
            if not j.get('bittorrent'):opts.pop('seed-time',None)
            try:self.rpc('changeOption',j['gid'],opts)
            except Exception:failures+=1
        self.rpc('saveSession')
        queue=Path('/admin-control')
        if queue.is_dir():
            tmp=queue/'options.tmp';tmp.write_text(json.dumps(options));os.replace(tmp,queue/'options.request')
        actual=self.rpc('getGlobalOption');pending=[]
        if isinstance(actual,dict):
            if actual.get('disk-cache')!=str(int(s['options']['disk-cache'][:-1])*1024*1024):pending.append('磁盘缓存')
            if actual.get('enable-dht')!=s['options']['enable-dht']:pending.append('DHT')
        s.update(applied=True,task_update_failures=failures,restart_required=pending);self.acceleration_store(s)
    @serialized
    def acceleration_save(self,d):
        s=self.acceleration_status();o=dict(DEFAULTS)
        values=d.get('options',{})
        if not isinstance(values,dict):raise self.problem('设置格式不正确')
        ranges={'max-concurrent-downloads':(1,20),'bt-max-peers':(10,300),'max-connection-per-server':(1,16),'seed-time':(0,1440)}
        for k,(lo,hi) in ranges.items():
            try:n=int(values.get(k,o[k]))
            except (ValueError,TypeError):raise self.problem('请输入有效数字')
            if not lo<=n<=hi:raise self.problem('设置数值超出允许范围')
            o[k]=str(n)
        import re
        for k in ('max-overall-download-limit','max-overall-upload-limit'):
            v=str(values.get(k,o[k])).upper()
            if not re.fullmatch(r'(0|[1-9][0-9]{0,5}[KM]?)',v):raise self.problem('限速请输入 0、50K 或 5M 等数值')
            o[k]=v
        if str(values.get('disk-cache','64M')) not in ('16M','32M','64M','128M'):raise self.problem('请选择有效缓存大小')
        o['disk-cache']=str(values.get('disk-cache','64M'))
        for k in ('enable-dht','enable-peer-exchange'):o[k]='true' if values.get(k,'true') in (True,'true') else 'false'
        custom=d.get('custom','')
        if not isinstance(custom,str) or len(custom)>16000:raise self.problem('自定义地址过长')
        if any(l.strip() and not trackers(l) for l in custom.splitlines()):raise self.problem('自定义 Tracker 地址格式不正确')
        s.update(options=o,auto=bool(d.get('auto',True)),custom=custom,applied=False)
        self.acceleration_apply(s);return s
    @serialized
    def acceleration_sync(self):
        s=self.acceleration_status();s['last_attempt']=time.time();found=[];results=[]
        for url in SOURCES:
            try:
                req=urllib.request.Request(url,headers={'User-Agent':'NovaDownloadVault/1.9.1'})
                with urllib.request.urlopen(req,timeout=15) as r:body=r.read(262145)
                if len(body)>262144:raise ValueError('oversize')
                links=trackers(body.decode('utf-8'))
                if not links:raise ValueError('empty')
                found.extend(links);results.append({'url':url,'ok':True,'count':len(links)})
            except Exception:results.append({'url':url,'ok':False,'count':0})
        s['sources_status']=results
        if found:
            s.update(applied=False,trackers=list(dict.fromkeys(found)),last_success=time.time(),error='' if all(r['ok'] for r in results) else '一个来源暂时不可用，已使用另一个来源')
        else:s['error']='同步失败，保留上次有效列表，稍后自动重试'
        self.acceleration_store(s)
        if found:self.acceleration_apply(s)
        return s
    def acceleration_worker(self):
        while not self.stop.is_set():
            try:
                s=self.acceleration_status()
                if not getattr(self,'acceleration_started',False) or not s.get('applied'):self.acceleration_apply(s);self.acceleration_started=True
                if s['auto'] and time.time()-s.get('last_attempt',0)>(86400 if not s.get('error') else 3600):self.acceleration_sync()
            except Exception:
                s=self.acceleration_status();s['error']='下载器暂时无法连接，后台会自动重试';self.acceleration_store(s)
            self.stop.wait(60)

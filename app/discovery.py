"""Bounded, persisted keyword discovery. No remote JavaScript execution or credentials."""
import base64, collections, html, json, re, threading, time
import urllib.robotparser
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote
from explorer import (fetch_public, normalize_url, ExploreError, Reader, candidate_kind,
                      download_identity, link_id, label_for, now, uid, dumps)

from discovery_store import DiscoveryStore, page_publication, publication, timestamp, scope_key, CST
from datetime import datetime, timedelta

SEARCH_NAMES={'q','s','query','search','keyword','keywords','wd','search_query','searchword'}
MEDIA=re.compile(r'\.(mp4|m4v|webm|mkv|mov|mp3|m4a|aac|ogg|flac|wav|m3u8|mpd)(?:$|[?#])',re.I)
TEXT_URL=re.compile(r'https?://[^\s<>"\'\\]+',re.I)
MAX_PAGES=18
MAX_REQUESTS=64
MAX_SECONDS=180

class CrawlerRobots(urllib.robotparser.RobotFileParser):
    """Honor wildcard/end rules and longest-match precedence missing in stdlib matching."""
    def can_fetch(self,useragent,url):
        if self.disallow_all:return False
        if self.allow_all:return True
        groups=self.entries+([self.default_entry] if self.default_entry else [])
        selected=[];best=-1
        for group in groups:
            score=max((0 if a=='*' else len(a) for a in group.useragents if a=='*' or a.lower() in useragent.lower()),default=-1)
            if score>best:best=score;selected=[group]
            elif score==best and score>=0:selected.append(group)
        p=urlsplit(url);target=unquote(p.path or '/')+('?' + unquote(p.query) if p.query else '')
        matches=[]
        for group in selected:
            for rule in group.rulelines:
                path=unquote(rule.path)
                if not path:continue
                exact=path.endswith('$');pattern=path[:-1] if exact else path
                regex='^'+re.escape(pattern).replace(r'\*','.*')+('$' if exact else '')
                if re.search(regex,target):matches.append((len(pattern.replace('*','')),rule.allowance))
        return max(matches)[1] if matches else True

def absolute(value,base):
    try:return normalize_url(value,base)
    except ExploreError:return None

def same_site(a,b):return urlsplit(a).netloc.lower()==urlsplit(b).netloc.lower()
def decode(response):
    raw=response.get('body',b'');header=response.get('headers',{}).get('content-type','')
    m=re.search(r'charset\s*=\s*["\']?([\w-]+)',header,re.I) or re.search(r'charset\s*=\s*["\']?([\w-]+)',raw[:8192].decode('ascii','ignore'),re.I)
    encoding=m.group(1) if m else 'utf-8'
    try:return raw.decode('gb18030' if encoding.lower() in ('gbk','gb2312') else encoding,errors='replace')
    except LookupError:return raw.decode('utf-8',errors='replace')

def expanded_template(template,keyword,base):
    if not isinstance(template,str) or not re.search(r'\{(?:query|searchTerms|search_term_string)\}',template):return None
    value=re.sub(r'\{(?:query|searchTerms|search_term_string)\}',quote(keyword,safe=''),html.unescape(template))
    value=re.sub(r'\{[^{}]+\?\}','',value)
    if '{' in value or '}' in value:return None
    return absolute(value,base)

def media_kind(url):
    m=MEDIA.search(url)
    if m:return 'hls' if m[1].lower()=='m3u8' else 'dash' if m[1].lower()=='mpd' else 'media'
    return 'media'

class Scan(HTMLParser):
    def __init__(self,url,keyword=''):
        super().__init__(convert_charrefs=True)
        self.base=url;self.keyword=keyword;self.form=None;self.forms=[];self.scripts=[];self.opensearch=[]
        self.media=[];self.embeds=[];self.apis=[];self.notes=[];self.buffers=[];self.capture=None;self.title='';self.in_title=False
    def add_media(self,value,evidence,force=False):
        if not isinstance(value,str):return
        value=html.unescape(value.strip()).replace('\\/','/').replace('\\u0026','&')
        if value.startswith('blob:'):self.notes.append('发现 blob 播放地址，需要浏览器运行后解析');return
        if value.startswith(('http%3A','https%3A','http%3a','https%3a')):value=unquote(value)
        url=absolute(value,self.base)
        if url and (force or MEDIA.search(url)):
            self.media.append({'url':url,'title':label_for(url),'kind':media_kind(url),'evidence':evidence})
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='title':self.in_title=True
        if tag=='form':self.form={'action':a.get('action') or self.base,'method':a.get('method','get').lower(),'fields':[],'search':None}
        if tag=='input' and self.form:
            name=a.get('name','');typ=a.get('type','text').lower()
            if name and (typ=='search' or name.lower() in SEARCH_NAMES):self.form['search']=name
            elif name and typ=='hidden' and not re.search('token|csrf|password',name,re.I):self.form['fields'].append((name,a.get('value','')))
        if tag=='link' and 'search' in a.get('rel','').split() and 'opensearch' in a.get('type',''):
            u=absolute(a.get('href',''),self.base)
            if u:self.opensearch.append(u)
        if tag=='script':
            if a.get('src'):
                u=absolute(a['src'],self.base)
                if u:self.scripts.append(u)
            self.capture=[]
        if tag in ('video','audio','source'):
            for key in ('src','data-src'):self.add_media(a.get(key,''),'HTML '+tag,True) if a.get(key) else None
        if tag=='meta' and a.get('property',a.get('name','')).lower() in ('og:video','og:video:url','og:video:secure_url','og:audio','twitter:player:stream'):
            self.add_media(a.get('content',''),'媒体元数据',True)
        if tag=='iframe' and a.get('src'):
            u=absolute(a['src'],self.base)
            if u:self.embeds.append({'url':u,'title':a.get('title','内嵌播放器'),'embed':True})
    def handle_endtag(self,tag):
        if tag=='title':self.in_title=False
        if tag=='form' and self.form:
            if self.form['search']:
                if self.form['method']=='get':self.forms.append(self.form)
                else:self.notes.append('发现 POST 搜索表单，本版需要提供 GET 搜索模板')
            self.form=None
        if tag=='script' and self.capture is not None:self.buffers.append(''.join(self.capture));self.capture=None
    def handle_data(self,data):
        if self.in_title:self.title+=data
        if self.capture is not None:self.capture.append(data)
    def scan_data(self,source):
        # Read declared data, never eval JavaScript. JSON strings can encode relative media URLs.
        source=source.replace('\\/','/').replace('\\u0026','&').replace('\\u003d','=')
        for match in TEXT_URL.finditer(source):self.add_media(match[0],'页面或脚本中的媒体 URL')
        for match in re.finditer(r'''["'](?:contentUrl|file|src|url|play_url|playUrl|video_url|videoUrl|hls|dash)["']\s*:\s*["']([^"'\n]{1,8192})["']''',source):
            value=match[1]
            if not value.startswith(('http','/','blob:')):
                try:
                    decoded=base64.b64decode(value,validate=True).decode('utf-8')
                    decoded=unquote(decoded)
                    if decoded.startswith(('http://','https://')):value=decoded
                except (ValueError,UnicodeError):pass
            if value.startswith(('http','/','blob:')):self.add_media(value,'播放器配置',bool(re.search('contentUrl|play_url|playUrl|video_url|videoUrl|hls|dash',match[0])))
        # Public, explicitly declared GET URLs; no endpoint wordlist probing.
        for match in re.finditer(r'''["']([^"'\n]{1,2048}(?:[?&](?:q|s|query|keyword|wd|search)=))["']\s*\+''',source):
            u=absolute(match[1]+quote(self.keyword,safe=''),self.base)
            if u and same_site(u,self.base):self.apis.append({'url':u,'method':'脚本声明的搜索请求','evidence':match[1]+'{query}'})
        for match in re.finditer(r'''["']([^"'\n]*\{(?:query|searchTerms|search_term_string)\}[^"'\n]*)["']''',source):
            u=expanded_template(match[1],self.keyword,self.base)
            if u:self.apis.append({'url':u,'method':'声明的搜索模板','evidence':match[1]})
    def finish(self,source):
        parsed=urlsplit(self.base);params=parse_qsl(parsed.query,keep_blank_values=True)
        if any(k.lower() in SEARCH_NAMES for k,v in params):
            updated=[(k,self.keyword if k.lower() in SEARCH_NAMES else v) for k,v in params]
            self.apis.append({'url':urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(updated),'')),'method':'目标地址已有搜索参数','evidence':self.base})
        for data in self.buffers:self.scan_data(data)
        self.scan_data(source)
        for form in self.forms:
            action=absolute(form['action'],self.base)
            if not action:continue
            p=urlsplit(action);fields=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k!=form['search']]
            fields+=form['fields']+[(form['search'],self.keyword)]
            u=urlunsplit((p.scheme,p.netloc,p.path,urlencode(fields),''))
            self.apis.append({'url':u,'method':'站内 GET 搜索表单','evidence':form['search']})
        reader=Reader(self.base);reader.feed(source);reader.close();page=reader.finish(source)
        return {'title':self.title.strip()[:200] or page['title'],'links':page['links']+self.embeds,
                'candidates':page['candidates']+self.media,'apis':self.apis,'scripts':self.scripts,
                'opensearch':self.opensearch,'notes':list(dict.fromkeys(self.notes))}

def scan_response(response,keyword):
    source=decode(response);parser=Scan(response['url'],keyword)
    if response['is_html']:parser.feed(source);parser.close()
    out=parser.finish(source)
    out.update(page_publication(source) if response['is_html'] else {})
    if 'json' in response['headers'].get('content-type',''):
        try:data=json.loads(source)
        except ValueError:data=None
        count=[0]
        def walk(obj,depth=0):
            if depth>14 or count[0]>4000:return
            count[0]+=1
            if isinstance(obj,dict):
                if obj.get('rel') in ('next','prev','previous','self'):return
                title=str(obj.get('title',obj.get('name','API 结果')))[:180]
                for key,value in obj.items():
                    if isinstance(value,str) and key.lower() in ('url','href','link','detail_url','embedurl','webpage_url','contenturl','file','play_url'):
                        u=value if candidate_kind(value)=='magnet' else absolute(value,response['url'])
                        if u:
                            date=publication(obj) or {}
                            if MEDIA.search(u) or (candidate_kind(u) and not urlsplit(u).path.lower().endswith('.json')):out['candidates'].append({'url':u,'title':title,'kind':media_kind(u) if MEDIA.search(u) else candidate_kind(u),'evidence':'API 结果',**date})
                            else:out['links'].append({'url':u,'title':title,'api_result':True,**date})
                    walk(value,depth+1)
            elif isinstance(obj,list):
                for v in obj:walk(v,depth+1)
        walk(data)
    return out

class Halt(Exception):pass
class Discovery:
    def __init__(self,vault,fetcher=fetch_public):
        self.vault=vault;self.fetcher=fetcher;self.control=threading.Lock();self.events={}
        self.vault.db.execute('CREATE TABLE IF NOT EXISTS discovery_jobs(id TEXT PRIMARY KEY,created_at TEXT,status TEXT,data TEXT)')
        self.store=DiscoveryStore(vault)
        for row in self.vault.rows("SELECT id,data FROM discovery_jobs WHERE status IN ('running','queued')"):
            data=json.loads(row['data']);data.update(status='queued',message='服务恢复，等待 NAS 继续探索')
            self.save(data)
    def recover(self):
        # Invoked by the NAS worker, never by browser polling.
        with self.control:
            if self.events:return
            rows=self.vault.rows("SELECT data FROM discovery_jobs WHERE status='queued' ORDER BY created_at LIMIT 1")
            if not rows:return
            job=json.loads(rows[0]['data'])
            attempts=job.get('recoveries',0)+1
            if attempts>3:
                job.update(status='interrupted',message='服务连续重启，已保留结果；请重新开始')
                self.save(job);return
            job.update(recoveries=attempts,requests=0,pages=[],skipped_known=0,skipped_date=0,unknown_dates=0)
            job['notes'].append('服务重启后自动重新搜索；已有候选保留并去重')
            self.events[job['id']]=threading.Event();self.save(job)
            threading.Thread(target=self.run,args=(job,),daemon=True).start()
    def save(self,job):
        with self.vault.lock:
            self.vault.db.execute('INSERT INTO discovery_jobs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data',(job['id'],job['created_at'],job['status'],dumps(job)))
    def get(self,ident):
        with self.vault.lock:
            row=self.vault.db.execute('SELECT data FROM discovery_jobs WHERE id=?',(ident,)).fetchone()
            if not row:raise self.vault.problem('探索任务不存在',404)
            job=json.loads(row[0])
            for c in job['candidates']:c['previous']=self.vault.previous_downloads(c['url'])
            return job
    def history(self):
        with self.vault.lock:
            jobs=[json.loads(r['data']) for r in self.vault.rows('SELECT data FROM discovery_jobs ORDER BY created_at DESC LIMIT 30')]
            return [{k:j[k] for k in ('id','site','keyword','created_at','status','message')}|{'count':len(j['candidates']),'finished_at':j.get('finished_at'),'published_after':j.get('published_after',''),'incremental_since':j.get('incremental_since','')} for j in jobs]
    def start(self,data,background=True):
        try:site=normalize_url(data.get('site'))
        except ExploreError as e:raise self.vault.problem(str(e))
        keyword=str(data.get('keyword','')).strip();template=str(data.get('template','')).strip()
        depth=data.get('depth',2)
        if type(depth) is not int or not 1<=depth<=4:raise self.vault.problem('探索深度必须为 1–4 层')
        browse=data.get('browse',False) is True
        if (not keyword and not browse) or len(keyword)>100:raise self.vault.problem('请输入 1–100 字的关键词')
        if template and not expanded_template(template,keyword,site):raise self.vault.problem('搜索模板需包含 {query}，使用公开 HTTP/HTTPS 地址')
        after=data.get('published_after','') or ''
        if not isinstance(after,str):raise self.vault.problem('请选择有效的发布日期')
        cutoff=None
        if after:
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',after):raise ValueError()
                cutoff=timestamp((datetime.strptime(after,'%Y-%m-%d')+timedelta(days=1)).isoformat())
            except (ValueError,OverflowError):raise self.vault.problem('请选择有效的发布日期')
        unknown=data.get('include_unknown',False)
        if type(unknown) is not bool:raise self.vault.problem('未知发布日期选项无效')
        since='';baseline=data.get('baseline_id','');mode=data.get('mode','full')
        if mode not in ('full','incremental'):raise self.vault.problem('探索方式无效')
        if mode=='incremental':
            if baseline:
                old=self.get(baseline)
                if old['status'] in ('running','queued'):raise self.vault.problem('基准任务尚未结束')
                if scope_key(old['site'],old['keyword'])!=scope_key(site,keyword):raise self.vault.problem('增量基准必须来自同一网站和关键词')
                since=old.get('finished_at') or old['created_at']
            elif data.get('incremental_since'):
                since=timestamp(data['incremental_since'])
                if not since:raise self.vault.problem('增量起始时间无效')
            else:
                prior=self.store.latest(site,keyword)
                if not prior:raise self.vault.problem('暂无同一网站和关键词的完整探索记录，请先执行一次普通探索')
                since=prior['finished_at'];baseline=prior['job_id']
            if since>now():raise self.vault.problem('增量起始时间不能晚于当前时间')
        with self.control:
            if self.events or self.vault.rows("SELECT id FROM discovery_jobs WHERE status='queued'"):raise self.vault.problem('已有探索任务正在运行，请等待或先停止',409)
            job={'id':uid(),'site':site,'keyword':keyword,'browse':browse,'template':template,'depth':depth,'published_after':after,'published_cutoff':cutoff,'include_unknown':unknown,'mode':mode,'incremental_since':since,'baseline_id':baseline,'skipped_known':0,'skipped_date':0,'unknown_dates':0,'created_at':now(),'status':'queued','message':'准备发现搜索入口','requests':0,'pages':[], 'endpoints':[],'candidates':[],'notes':[]}
            self.events[job['id']]=threading.Event();self.save(job)
            with self.vault.lock:self.vault.db.execute('DELETE FROM discovery_jobs WHERE id NOT IN (SELECT id FROM discovery_jobs ORDER BY created_at DESC LIMIT 30)')
        if background:threading.Thread(target=self.run,args=(job,),daemon=True).start()
        else:self.run(job)
        return self.get(job['id'])
    def cancel(self,ident):
        with self.control:
            event=self.events.get(ident)
            if event:event.set()
        return {'ok':True}
    def run(self,job):
        deadline=time.monotonic()+MAX_SECONDS;event=self.events[job['id']];robots={};user_searches=set();seen=set();candidate_ids={download_identity(c['url']) for c in job['candidates']};strict_keyword=False;last_request={};observed=set();known_pages=self.store.known_pages(job)
        def checkpoint():
            if event.is_set():raise Halt('已停止，已发现的结果已保留')
            if time.monotonic()>deadline:raise Halt('已达到 3 分钟时限，保留当前结果')
            if job['requests']>=MAX_REQUESTS:raise Halt('已达到请求上限，保留当前结果')
        def request(url,**kw):
            checkpoint()
            p=urlsplit(url);origin=p.scheme+'://'+p.netloc;rules=robots.get(origin)
            delay=rules.crawl_delay('NovaVaultExplorer') if rules else None
            if delay:
                wait=max(0,last_request.get(origin,0)+delay-time.monotonic())
                if wait:event.wait(min(wait,max(0,deadline-time.monotonic())))
                checkpoint()
            last_request[origin]=time.monotonic();job['requests']+=1;self.save(job)
            return self.fetcher(url,**kw)
        def allowed(url):
            p=urlsplit(url);origin=p.scheme+'://'+p.netloc
            if origin not in robots:
                try:r=request(origin+'/robots.txt',read_text=True);source=decode(r)
                except ExploreError as e:
                    code=e.status
                    if code is None:
                        match=re.search(r'HTTP (\d{3})',str(e));code=int(match[1]) if match else None
                    # RFC 9309 2.3.1.3: unavailable robots (4xx) is not a Disallow rule.
                    # 429 remains a hard stop; destination pages still honor their own HTTP errors.
                    if code and 400<=code<500 and code!=429:source=''
                    else:
                        robots[origin]=None
                        raise ExploreError('无法确认网站自动访问规则：'+str(e))
                rp=CrawlerRobots();rp.parse(source.splitlines());robots[origin]=rp
            if robots[origin] is None:raise ExploreError('网站自动访问规则暂时不可达，本次停止访问该站点')
            if not robots[origin].can_fetch('NovaVaultExplorer',url):
                if url in user_searches:note('用户主动站内搜索：读取网站表单声明的公开 GET 搜索页（不作为搜索引擎索引）')
                else:raise ExploreError('网站 robots.txt 不允许自动访问该地址')
        def fetch_doc(url):allowed(url);return request(url,read_text=True)
        def note(text):
            if text not in job['notes']:job['notes'].append(text)
        def collect(scan,url,depth,trail):
            for raw in sorted(scan['candidates'],key=lambda c:0 if c.get('published_at') else 1):
                if len(job['candidates'])>=80:note('候选已达到 80 条上限');break
                c=dict(raw)
                if strict_keyword and depth==0 and not any(term in (c.get('title','')+' '+unquote(c['url'])+' '+scan['title']).lower() for term in job['keyword'].lower().split()):continue
                # Keep image candidates too; cap decoration so media still has room.
                if re.search(r'\.(css|js|json)(?:$|[?#])',c['url'],re.I):continue
                if re.search(r'\.(png|jpe?g|gif|webp)(?:$|[?#])',c['url'],re.I) and sum(bool(re.search(r'\.(png|jpe?g|gif|webp)(?:$|[?#])',x['url'],re.I)) for x in job['candidates'])>=20:continue
                if MEDIA.search(c['url']):
                    c['kind']=media_kind(c['url'])
                    if scan.get('title'):c['title']=scan['title']
                identity=download_identity(c['url'])
                if identity in candidate_ids or identity in observed:continue
                observed.add(identity)
                if not c.get('published_at') and scan.get('published_at'):
                    c.update(published_at=scan['published_at'],publication_source=scan.get('publication_source','页面发布日期'))
                record=self.store.observe(job,c,now());c.update(record)
                if job.get('incremental_since') and c['first_seen']<=job['incremental_since']:
                    job['skipped_known']=job.get('skipped_known',0)+1;continue
                cutoff=job.get('published_cutoff')
                if not c.get('published_at'):job['unknown_dates']=job.get('unknown_dates',0)+1
                if cutoff and ((c.get('published_at') and c['published_at']<cutoff) or (not c.get('published_at') and not job.get('include_unknown'))):
                    job['skipped_date']=job.get('skipped_date',0)+1;continue
                candidate_ids.add(identity);c.update(id=link_id(c['url']),source=url,depth=depth,trail=trail,
                    verification='format' if c['kind']=='magnet' else 'pending',evidence=c.get('evidence','页面链接'),checked_at='',downloadable=c['kind']=='magnet')
                c.pop('verified',None);job['candidates'].append(c)
            for n in scan['notes']:note(n)
        def inspect(url,depth,trail,collect_candidates=True,inherited=None):
            checkpoint();url=normalize_url(url)
            if url in seen:return None
            seen.add(url);entry={'url':url,'depth':depth,'status':'reading','title':'','error':''};job['pages'].append(entry);job['message']='正在探索第 '+str(depth)+' 层';self.save(job)
            try:
                response=fetch_doc(url);entry['url']=response['url'];entry['status']='ok';seen.add(response['url'])
                ctype=response['headers'].get('content-type','')
                if not response['is_html'] and not any(t in ctype for t in ('json','javascript','xml','text/')):
                    scan={'title':label_for(url),'links':[],'candidates':[{'url':response['url'],'kind':media_kind(url) if MEDIA.search(url) or ctype.startswith(('video/','audio/')) else 'direct','title':label_for(url)}],'apis':[],'scripts':[],'opensearch':[],'notes':[]}
                else:scan=scan_response(response,job['keyword'])
                if inherited and not scan.get('published_at'):scan.update(inherited)
                entry['title']=scan['title'];self.store.page_seen(job,response['url'],now())
                if collect_candidates:collect(scan,response['url'],depth,trail)
                self.save(job);return scan,response['url']
            except ExploreError as e:entry.update(status='error',error=str(e));self.save(job);return None
        try:
            job['status']='running';self.save(job)
            root=inspect(job['site'],0,[job['site']],False)
            if not root:
                if not job['template']:raise ExploreError('目标网站读取失败，请查看访问记录')
                note('目标页读取失败，继续尝试指定的搜索模板')
                root=({'apis':[],'scripts':[],'opensearch':[]},job['site'])
            scan,site=root;endpoints=sorted(([] if job.get('browse') else scan['apis']),key=lambda e:0 if e['method']=='站内 GET 搜索表单' else 1)
            for u in (scan['opensearch'][:2] if not endpoints and not job.get('browse') else []):
                try:
                    source=decode(fetch_doc(u))
                    if '<!DOCTYPE' in source.upper() or '<!ENTITY' in source.upper():continue
                    xml=ET.fromstring(source)
                    for el in xml.iter():
                        if el.tag.split('}')[-1]=='Url' and el.attrib.get('method','GET').upper()=='GET':
                            target=expanded_template(el.attrib.get('template',''),job['keyword'],u)
                            if target:endpoints.append({'url':target,'method':'OpenSearch 声明','evidence':u})
                except (ExploreError,ET.ParseError) as e:note('搜索描述读取失败：'+str(e))
            for u in (list(dict.fromkeys(scan['scripts']))[:3] if not endpoints and not job.get('browse') else []):
                if not same_site(u,site):continue
                try:
                    result=scan_response(fetch_doc(u),job['keyword']);endpoints.extend(result['apis'])
                except ExploreError as e:note('站内脚本读取失败：'+str(e))
            if job['template']:endpoints.insert(0,{'url':expanded_template(job['template'],job['keyword'],site),'method':'指定搜索模板','evidence':job['template']})
            unique=[]
            for ep in endpoints:
                if len(unique)>=3:break
                if ep['url'] not in [e['url'] for e in unique] and (same_site(ep['url'],site) or ep['method']=='指定搜索模板'):unique.append(ep)
            job['endpoints']=unique
            user_searches.update(ep['url'] for ep in unique if ep['method']=='站内 GET 搜索表单')
            self.save(job)
            queue=collections.deque()
            if unique:
                for ep in unique:
                    seen.discard(ep['url']);queue.append((ep['url'],0,[ep['url']],{}))
            else:
                strict_keyword=not job.get('browse')
                note('从当前网页出发，优先探索详情和播放页' if job.get('browse') else '未发现公开 GET 搜索入口；已按关键词筛选目标页链接，可在高级选项填写搜索模板')
                queue.append((site,0,[site],{}));seen.discard(site)
            while queue and len(job['pages'])<MAX_PAGES:
                url,depth,trail,inherited=queue.popleft();found=inspect(url,depth,trail,inherited=inherited)
                if not found:continue
                page,final=found
                # Read a small number of explicitly referenced player scripts on the deepest pages.
                if depth==job.get('depth',2):
                    for script in list(dict.fromkeys(page['scripts']))[:1]:
                        if same_site(script,final):
                            try:collect(scan_response(fetch_doc(script),job['keyword']),final,depth,trail)
                            except ExploreError as e:note('播放器配置读取失败：'+str(e))
                if depth>=job.get('depth',2):continue
                links=[]
                for link in page['links']:
                    u=link['url']
                    if not u.startswith(('http://','https://')) or u in seen or any(x[1]['url']==u for x in links):continue
                    if MEDIA.search(u) or (candidate_kind(u) and not (link.get('api_result') and urlsplit(u).path.lower().endswith('.json'))):continue
                    if not (same_site(u,site) or link.get('embed') or link.get('api_result')):continue
                    if re.search(r'logout|signout|delete|remove|购物车|退出',u+' '+link.get('title',''),re.I):continue
                    text=unquote(u)+' '+link.get('title','');relevance=sum(1 for term in job['keyword'].lower().split() if term in text.lower())
                    if depth==0 and not relevance and not unique and not job.get('browse'):continue
                    rank=(1000 if re.search(r'/play/|/watch/|立即播放',text,re.I) else 100 if re.search(r'/video/|/detail/',u,re.I) else 0)+relevance*100+(20 if job.get('incremental_since') and u not in known_pages else 0)+(5 if re.search(r'play|watch|video|播放|详情',text,re.I) else 0)+(8 if link.get('embed') else 0)
                    links.append((rank,link))
                for _,link in sorted(links,key=lambda x:x[0],reverse=True)[:6]:
                    date={k:link[k] for k in ('published_at','publication_source') if k in link}
                    if not date and depth>0 and (link.get('embed') or re.search(r'/play/|/watch/',link['url'],re.I)):date={k:page[k] for k in ('published_at','publication_source') if k in page}
                    if re.search(r'/play/|/watch/',link['url'],re.I):queue.appendleft((link['url'],depth+1,trail+[link['url']],date))
                    else:queue.append((link['url'],depth+1,trail+[link['url']],date))
            if queue:note('达到页面上限，剩余页面未继续探索')
            job['message']='正在验证媒体响应';self.save(job)
            # Prefer media over miscellaneous downloads when probe budget is limited.
            candidates=sorted(job['candidates'],key=lambda c:0 if c['kind'] in ('media','hls','dash') else 1)
            for c in candidates[:16]:
                checkpoint()
                if c['kind']=='magnet':continue
                try:
                    r=request(c['url'],sample=True);ctype=r['headers'].get('content-type','').lower();raw=r.get('body',b'');text=raw.decode('utf-8','ignore').lstrip('\ufeff \r\n');c['checked_at']=now();c['resolved_url']=r['url'];c['content_type']=ctype
                    if text.startswith('#EXTM3U'):
                        c.update(kind='hls',verification='manifest',downloadable=False,detail='已验证 HLS 播放列表；分片可用性未逐一验证，不能直接当完整视频下载')
                        if re.search(r'#EXT-X-(?:SESSION-)?KEY:.*METHOD=(?!NONE)',text):c['detail']='加密 HLS 播放列表；不自动下载或解密';c['verification']='protected'
                    elif '<MPD' in text[:2048]:
                        c.update(kind='dash',verification='manifest',downloadable=False,detail='已验证 DASH 清单；不是单一视频文件')
                        if 'ContentProtection' in text:c.update(verification='protected',detail='包含受保护媒体声明，不自动处理')
                    elif r['is_html'] or re.match(r'(?is)\s*(?:<!doctype\s+html|<html)',text):c.update(verification='page',downloadable=False,detail='实际返回网页，不是播放文件')
                    elif (len(raw)>8 and raw[4:8]==b'ftyp') or raw.startswith((b'\x1aE\xdf\xa3',b'OggS',b'ID3',b'fLaC')) or (ctype.startswith(('video/','audio/')) and c['kind'] not in ('hls','dash')):
                        c.update(kind='media',verification='media',downloadable=True,detail='媒体响应已验证；播放和下载仍受源站时效限制')
                    elif c['kind'] in ('media','hls','dash'):c.update(verification='unknown',downloadable=False,detail='尚未确认媒体类型，可复制地址或打开来源页')
                    elif not any(t in ctype for t in ('json','javascript','xml')) and raw:c.update(verification='file',downloadable=True,detail='文件响应已验证')
                    else:c.update(verification='unknown',downloadable=False,detail='未能确认可下载文件')
                except ExploreError as e:c.update(verification='failed',downloadable=False,detail=str(e))
                self.save(job)
            if len(candidates)>16:note('仅验证前 16 个候选，其余保留为未验证地址')
            failed_pages=sum(p['status']=='error' for p in job['pages'])
            job.update(status='partial' if queue or failed_pages else 'done',message=('已达到 18 页上限，' if queue else '部分页面访问失败，' if failed_pages else '探索完成，')+'发现 '+str(len(job['candidates']))+' 个候选'+('；可更换关键词或搜索模板' if not job['candidates'] else ''))
        except Halt as e:job.update(status='cancelled' if event.is_set() else 'partial',message=str(e))
        except ExploreError as e:job.update(status='error',message=str(e))
        except Exception as e:
            print('discovery error:',type(e).__name__,flush=True);job.update(status='error',message='探索遇到异常，已保留现有结果')
        finally:
            job['finished_at']=now()
            if job['status']=='done':self.store.finish(job,job['finished_at'])
            for entry in job['pages']:
                if entry['status']=='reading':entry.update(status='error',error=job['message'])
            self.save(job)
            with self.control:self.events.pop(job['id'],None)
    def download(self,data):
        job=self.get(data.get('job_id'));c=next((c for c in job['candidates'] if c['id']==data.get('link_id')),None)
        if not c or not c.get('downloadable'):raise self.vault.problem('该地址尚未验证为可下载文件；播放清单不等于完整视频',409)
        return self.vault.explorer.queue_candidate(c,data)

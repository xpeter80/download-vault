"""Manual website reader and link extraction; remote code is never executed."""
import hashlib, html, http.client, ipaddress, re, socket, ssl, threading, time, json, secrets, datetime as dt
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote, parse_qs

def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def uid():return secrets.token_hex(12)
def dumps(v):return json.dumps(v,ensure_ascii=False,separators=(',',':'))

MAX_BODY=2*1024*1024
MAX_LINKS=600
MAX_VIEW=220000
FILE_EXT=re.compile(r'\.(?:iso|zip|rar|7z|tar|gz|bz2|xz|exe|msi|dmg|pkg|apk|deb|rpm|mp4|mkv|avi|mov|wmv|webm|m4v|mp3|flac|wav|m4a|aac|ogg|pdf|epub|mobi|azw3|docx?|xlsx?|pptx?|txt|csv|json|torrent|jpe?g|png|webp|gif)(?:$)',re.I)
MAGNET=re.compile(r'magnet:\?[^\s<>"\'\\]+',re.I)
PUBLIC_PORTS={80,443}
class ExploreError(Exception):
    def __init__(self,message,status=None):super().__init__(message);self.status=status

def normalize_url(value,base=None):
    if not isinstance(value,str):raise ExploreError('请输入网页网址')
    value=html.unescape(value.strip())
    if base:value=urljoin(base,value)
    elif '://' not in value and not value.lower().startswith('magnet:'):value='https://'+value
    if len(value)>8192 or any(ord(c)<32 for c in value) or '\\' in value:raise ExploreError('网址格式不正确')
    try:
        p=urlsplit(value)
        if p.scheme.lower() not in ('http','https') or not p.hostname or p.username is not None or p.password is not None:raise ValueError()
        if p.port and p.port not in PUBLIC_PORTS:raise ExploreError('网站探索仅支持 HTTP/HTTPS 的 80、443 端口')
        host=p.hostname.encode('idna').decode('ascii')
        netloc=('['+host+']' if ':' in host else host)+((':'+str(p.port)) if p.port else '')
        # Encode non-ASCII path/query without rewriting already percent-encoded signed URLs.
        from urllib.parse import quote
        return urlunsplit((p.scheme.lower(),netloc,quote(p.path or '/',safe="/%:@!$&'()*+,;=-._~"),quote(p.query,safe="%=&?/:@!$'()*+,;~-._"),''))
    except (ValueError,UnicodeError):raise ExploreError('请输入有效的 HTTP/HTTPS 网址，不支持网址中的账号密码')

def public_ips(host,port):
    try:ips=list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)))
    except socket.gaierror:raise ExploreError('无法解析网站域名')
    if not ips:raise ExploreError('网站域名没有可用地址')
    for value in ips:
        a=ipaddress.ip_address(value)
        if not a.is_global or a.is_multicast or a.is_reserved:raise ExploreError('不能浏览 NAS、局域网或其他非公网地址')
    return ips

class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self,host,port,ip,timeout):super().__init__(host,port,timeout=timeout);self.ip=ip
    def connect(self):self.sock=socket.create_connection((self.ip,self.port),self.timeout)
class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,port,ip,timeout):super().__init__(host,port,timeout=timeout,context=ssl.create_default_context());self.ip=ip
    def connect(self):
        sock=socket.create_connection((self.ip,self.port),self.timeout)
        try:self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
        except Exception:sock.close();raise

def fetch_public(url,method='GET',probe=False,read_text=False,sample=False):
    """Validate each redirect and pin TCP to validated DNS result (including TLS SNI)."""
    deadline=time.monotonic()+22
    for hop in range(6):
        url=normalize_url(url);p=urlsplit(url);port=p.port or (443 if p.scheme=='https' else 80)
        ips=public_ips(p.hostname,port)
        remaining=deadline-time.monotonic()
        if remaining<=0:raise ExploreError('网站响应超时，请稍后重试')
        conn=(PinnedHTTPS if p.scheme=='https' else PinnedHTTP)(p.hostname,port,ips[0],min(8,remaining))
        try:
            headers={'User-Agent':'Mozilla/5.0 (compatible; NovaVaultExplorer/1.1)','Accept':'text/html,application/xhtml+xml,*/*;q=0.8','Accept-Encoding':'identity','Connection':'close'}
            if probe and method=='GET':headers['Range']='bytes=0-0'
            if sample:headers['Range']='bytes=0-4095'
            conn.request(method,p.path+('?' + p.query if p.query else ''),headers=headers)
            response=conn.getresponse();h={k.lower():v for k,v in response.getheaders()}
            if response.status in (301,302,303,307,308):
                if not h.get('location'):raise ExploreError('网站跳转缺少目标地址')
                url=normalize_url(h['location'],url);continue
            if response.status in (405,501) and method=='HEAD':return fetch_public(url,'GET',True)
            if response.status>=400:raise ExploreError('网站返回 HTTP '+str(response.status)+'；可能需要登录、验证或暂时不可用，可打开原站',response.status)
            ctype=h.get('content-type','').lower()
            is_html='text/html' in ctype or 'application/xhtml+xml' in ctype
            body=b''
            if sample:
                body=response.read(4096)
            text_type=any(t in ctype for t in ('text/','json','javascript','xml','mpegurl','dash+xml'))
            if not sample and method!='HEAD' and not probe and (is_html or (read_text and text_type)):
                chunks=[];size=0
                while True:
                    if time.monotonic()>deadline:raise ExploreError('网页读取超时')
                    chunk=response.read(min(32768,MAX_BODY+1-size))
                    if not chunk:break
                    chunks.append(chunk);size+=len(chunk)
                    if size>MAX_BODY:raise ExploreError('网页超过 2 MB，无法内嵌阅读；请打开原站')
                body=b''.join(chunks)
            return {'url':url,'headers':h,'body':body,'is_html':is_html,'status':response.status}
        except ExploreError:raise
        except (OSError,http.client.HTTPException,ValueError):raise ExploreError('网站无法连接或证书验证失败，可稍后重试或打开原站')
        finally:conn.close()
    raise ExploreError('网站跳转次数过多')

def candidate_kind(url,download=False):
    if url.lower().startswith('magnet:?'):
        xt=parse_qs(urlsplit(url).query).get('xt',[])
        return 'magnet' if any(re.fullmatch(r'urn:btih:(?:[0-9a-fA-F]{40}|[A-Za-z2-7]{32})',x) for x in xt) else None
    try:p=urlsplit(url)
    except ValueError:return None
    if p.scheme not in ('http','https'):return None
    path=unquote(p.path)
    if path.lower().endswith('.torrent'):return 'torrent'
    if download or FILE_EXT.search(path):return 'direct'
    # Common file endpoints carry a filename in their query string.
    for k,vs in parse_qs(p.query).items():
        if k.lower() in ('file','filename','name','download') and any(FILE_EXT.search(x) for x in vs):return 'direct'
    return None

def download_identity(url):
    """Conservative URL matching; BT uses the content hash, not tracker/name."""
    import base64
    try:
        p=urlsplit(url.strip())
        if p.scheme.lower()=='magnet':
            for xt in parse_qs(p.query).get('xt',[]):
                if xt.lower().startswith('urn:btih:'):
                    value=xt[9:]
                    if re.fullmatch(r'[0-9a-fA-F]{40}',value):return 'btih:'+value.lower()
                    if re.fullmatch(r'[A-Za-z2-7]{32}',value):return 'btih:'+base64.b32decode(value.upper()).hex()
        return normalize_url(url)
    except (ExploreError,ValueError):return url.strip()

def link_id(url):return hashlib.sha256(url.encode()).hexdigest()[:24]
def label_for(url):
    if url.lower().startswith('magnet:'):return parse_qs(urlsplit(url).query).get('dn',['磁力链接'])[0][:180]
    return unquote(urlsplit(url).path.rstrip('/').split('/')[-1])[:180] or urlsplit(url).hostname

class Reader(HTMLParser):
    KEEP={'p','div','section','article','header','footer','main','nav','aside','span','h1','h2','h3','h4','h5','h6','ul','ol','li','dl','dt','dd','blockquote','pre','code','strong','b','em','i','small','sup','sub','table','thead','tbody','tfoot','tr','th','td','br','hr'}
    VOID={'br','hr'}
    SKIP={'script','style','noscript','iframe','object','template','svg','math','head'}
    def __init__(self,url):
        super().__init__(convert_charrefs=True);self.base=url;self.links=[];self.candidates={};self.out=[];self.stack=[];self.suppressed=[];self.title_mode=False;self.title=[];self.a=None;self.length=0;self.truncated=False
    def emit(self,text):
        if self.length+len(text)>MAX_VIEW:self.truncated=True;return
        self.out.append(text);self.length+=len(text)
    def candidate(self,url,label='',download=False):
        kind=candidate_kind(url,download)
        if kind and len(self.candidates)<300:
            ident=link_id(url);old=self.candidates.get(ident)
            if not old:self.candidates[ident]={'id':ident,'url':url,'title':label[:180] or label_for(url),'kind':kind,'verified':False}
            elif label:old['title']=label[:180]
    def resolve(self,value):
        value=html.unescape(value.strip()).replace('\\/','/')
        decoded=unquote(value)
        if decoded.lower().startswith('magnet:'):return decoded
        if value.lower().startswith('magnet:'):return value
        try:return normalize_url(value,self.base)
        except ExploreError:return None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='title':self.title_mode=True
        if tag=='base' and attrs.get('href'):
            u=self.resolve(attrs['href'])
            if u and not u.startswith('magnet:'):self.base=u
        # Extract media/data links even when source lives inside a non-rendered node.
        for k in ('href','src','data-src','data-href','data-url','data-download'):
            if attrs.get(k):
                u=self.resolve(attrs[k])
                if u:self.candidate(u,attrs.get('download') or attrs.get('title') or '', 'download' in attrs or k=='data-download' or (tag in ('video','audio','source') and k in ('src','data-src')))
        if tag in self.SKIP:self.suppressed.append(tag);return
        if self.suppressed:return
        if tag=='a':
            u=self.resolve(attrs.get('href','')) if attrs.get('href') else None
            if u and len(self.links)<MAX_LINKS:
                i=len(self.links);self.links.append({'url':u,'title':''});self.emit('<button type="button" class="site-link" data-explore-nav="'+str(i)+'">');self.a={'index':i,'url':u,'text':[]};self.stack.append(('a','button'))
            else:self.stack.append(('a','span'));self.emit('<span>')
        elif tag=='img':
            alt=attrs.get('alt','').strip()
            if alt:self.emit('<span class="image-alt">[图片：'+html.escape(alt[:120])+']</span>')
        elif tag in self.KEEP:
            target='div' if tag in ('main','nav','header','footer') else tag
            self.emit('<'+target+'>')
            if tag not in self.VOID:self.stack.append((tag,target))
    def handle_endtag(self,tag):
        if tag=='title':self.title_mode=False
        if self.suppressed:
            if tag==self.suppressed[-1]:self.suppressed.pop()
            return
        if tag=='a' and self.a:
            t=' '.join(''.join(self.a['text']).split())[:180];self.links[self.a['index']]['title']=t or label_for(self.a['url']);self.candidate(self.a['url'],t,bool(re.search(r'下载|附件|download',t,re.I)));self.a=None
        matches=[i for i,(source,_) in enumerate(self.stack) if source==tag]
        if matches:
            i=matches[-1]
            for _,target in reversed(self.stack[i:]):self.emit('</'+target+'>')
            del self.stack[i:]
    def handle_data(self,data):
        if self.title_mode:self.title.append(data)
        if self.suppressed:return
        if self.a:self.a['text'].append(data)
        self.emit(html.escape(data))
    def finish(self,source):
        # Magnet links often appear as plain text or inside JSON attributes/scripts.
        scan=html.unescape(source).replace('\\/','/').replace('\\u0026','&').replace('\\u003d','=')
        for m in MAGNET.finditer(scan):self.candidate(m.group(0).rstrip(').,;'))
        for m in re.finditer(r'https?://[^\s<>\"\'\\]+',scan):self.candidate(m.group(0).rstrip(').,;'))
        for _,target in reversed(self.stack):self.out.append('</'+target+'>')
        return {'title':' '.join(''.join(self.title).split())[:200] or urlsplit(self.base).hostname,'html':''.join(self.out),'links':self.links,'candidates':list(self.candidates.values()),'truncated':self.truncated}

def parse_page(response):
    if not response['is_html']:
        url=response['url'];ctype=response['headers'].get('content-type','未知类型');kind=candidate_kind(url) or 'direct'
        return {'title':label_for(url),'html':'<p>此地址直接返回文件，可以在“下载链接”中添加到 NAS。</p>','links':[],'candidates':[{'id':link_id(url),'url':url,'title':label_for(url),'kind':kind,'verified':True,'content_type':ctype}],'truncated':False,'content_type':ctype}
    raw=response['body'];header=response['headers'].get('content-type','');m=re.search(r'charset\s*=\s*["\']?([\w-]+)',header,re.I)
    if not m:m=re.search(r'charset\s*=\s*["\']?([\w-]+)',raw[:8192].decode('ascii','ignore'),re.I)
    enc=m.group(1) if m else 'utf-8'
    try:source=raw.decode('gb18030' if enc.lower() in ('gbk','gb2312') else enc,errors='replace')
    except LookupError:source=raw.decode('utf-8',errors='replace')
    parser=Reader(response['url']);parser.feed(source);parser.close();return parser.finish(source)

class Explorer:
    def __init__(self,vault,fetcher=fetch_public):
        self.vault=vault;self.fetcher=fetcher;self.slots=threading.BoundedSemaphore(3)
        self.vault.db.executescript('''CREATE TABLE IF NOT EXISTS explore_history(id TEXT PRIMARY KEY,url TEXT UNIQUE,title TEXT,visited_at TEXT,visit_count INTEGER DEFAULT 1,status TEXT,error TEXT DEFAULT '',snapshot TEXT);''')
    def visit(self,value):
        Problem=self.vault.problem
        try:url=normalize_url(value)
        except ExploreError as e:raise Problem(str(e))
        if not self.slots.acquire(blocking=False):raise Problem('正在读取其他网页，请稍后重试',429)
        try:
            result={'url':url,'title':urlsplit(url).hostname,'html':'','links':[],'candidates':[],'truncated':False,'status':'ok','error':''}
            try:
                response=self.fetcher(url);result.update(parse_page(response));result['url']=response['url']
                from discovery import scan_response
                scanned=scan_response(response,'');known={c['url'] for c in result['candidates']}
                for c in scanned['candidates']:
                    if c['url'] not in known:
                        known.add(c['url']);c['id']=link_id(c['url']);result['candidates'].append(c)
            except ExploreError as e:result.update(status='error',error=str(e))
            with self.vault.lock:
                old=self.vault.db.execute('SELECT id,visit_count FROM explore_history WHERE url=?',(url,)).fetchone()
                ident=old['id'] if old else uid();result.update(id=ident,visited_at=now(),requested_url=url)
                self.vault.db.execute('INSERT INTO explore_history(id,url,title,visited_at,visit_count,status,error,snapshot) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,visited_at=excluded.visited_at,visit_count=explore_history.visit_count+1,status=excluded.status,error=excluded.error,snapshot=excluded.snapshot',(ident,url,result['title'],result['visited_at'],1,result['status'],result['error'],dumps(result)))
                self.vault.db.execute('DELETE FROM explore_history WHERE id NOT IN (SELECT id FROM explore_history ORDER BY visited_at DESC LIMIT 200)')
            return self.annotate(result)
        finally:self.slots.release()
    def annotate(self,page):
        with self.vault.lock:
            for candidate in page['candidates']:
                candidate['previous']=self.vault.previous_downloads(candidate['url'])
        return page
    def history(self):
        with self.vault.lock:return self.vault.rows('SELECT id,url,title,visited_at,visit_count,status,error FROM explore_history ORDER BY visited_at DESC LIMIT 200')
    def saved(self,ident):
        Problem=self.vault.problem
        import json
        with self.vault.lock:
            r=self.vault.db.execute('SELECT snapshot FROM explore_history WHERE id=?',(ident,)).fetchone()
            if not r:raise Problem('浏览记录已不存在，请重新输入网址',404)
            return self.annotate(json.loads(r[0]))
    def remove(self,ident):
        Problem=self.vault.problem
        with self.vault.lock:
            if not ident:raise Problem('请选择要删除的历史记录')
            self.vault.db.execute('DELETE FROM explore_history WHERE id=?',(ident,))
    def download(self,data):
        Problem=self.vault.problem
        page=self.saved(data.get('page_id'));candidate=next((c for c in page['candidates'] if c['id']==data.get('link_id')),None)
        if not candidate:raise Problem('链接已变化，请重新读取网页',409)
        return self.queue_candidate(candidate,data)
    def queue_candidate(self,candidate,data):
        Problem=self.vault.problem
        if candidate['kind'] in ('hls','dash'):
            raise Problem('这是分片播放清单，可复制真实播放地址；当前 Aria2 不会合并为完整视频',409)
        # A retry must return the durable task even if the source URL has expired.
        key=str(data.get('request_key',''))
        with self.vault.lock:
            old=self.vault.db.execute('SELECT id,status,url FROM tasks WHERE request_key=?',(key,)).fetchone()
            if old:
                if old['url']!=candidate['url']:raise Problem('请求标识已用于其他链接，请刷新后重试',409)
                return {'id':old['id'],'status':old['status'],'name':candidate['title']}
        self.vault.check_download_history(candidate['url'],data.get('allow_duplicate',False))
        if candidate['kind']!='magnet':
            try:
                r=self.fetcher(candidate['url'],method='HEAD',probe=True)
                if r['is_html']:raise Problem('该链接实际返回网页，请在探索页继续浏览后再下载')
            except ExploreError as e:raise Problem(str(e))
        # Feed through the existing validated, idempotent download path. No arbitrary options.
        task=self.vault.create_task({'url':candidate['url'],'hidden':data.get('hidden',True),'request_key':data.get('request_key',''),'allow_duplicate':data.get('allow_duplicate',False),'tags':data.get('tags',[])})
        return {'id':task['id'],'status':task['status'],'name':candidate['title']}

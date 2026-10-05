"""Publication evidence and durable discovery identity ledger (SQLite, UTC storage)."""
import hashlib,json,re
from datetime import datetime,timedelta,timezone
from html.parser import HTMLParser
from explorer import download_identity
CST=timezone(timedelta(hours=8))
DATE_KEYS=('datePublished','published_at','publishedAt','date_published','pubdate','publish_time','publicationDate','uploadDate','published','publishDate','pubDate','upload_time')
def timestamp(value):
    if not isinstance(value,str):return None
    value=value.strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}(?:$|[T ])',value):return None
    try:
        d=datetime.fromisoformat(value.replace('Z','+00:00'))
        if d.tzinfo is None:d=d.replace(tzinfo=CST)
        return d.astimezone(timezone.utc).isoformat()
    except (ValueError,OverflowError):return None

def publication(obj):
    if not isinstance(obj,dict):return None
    for key in DATE_KEYS:
        value=timestamp(obj.get(key))
        if value:return {'published_at':value,'publication_source':key}
    return None

class PublicationParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.dates=[];self.buffer=None;self.article_depth=0
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='meta':
            key=a.get('property',a.get('name',a.get('itemprop','')))
            if key.lower() in ('article:published_time','datepublished','pubdate','publishdate','publish_time','uploaddate'):
                value=timestamp(a.get('content'))
                if value:self.dates.append({'published_at':value,'publication_source':'meta '+key})
        if tag=='time' and a.get('itemprop','').lower() in ('datepublished','uploaddate'):
            value=timestamp(a.get('datetime'))
            if value:self.dates.append({'published_at':value,'publication_source':'time '+a['itemprop']})
        if tag=='script' and a.get('type')=='application/ld+json':self.buffer=[]
    def handle_data(self,data):
        if self.buffer is not None:self.buffer.append(data)
    def handle_endtag(self,tag):
        if tag=='script' and self.buffer is not None:
            try:
                obj=json.loads(''.join(self.buffer));items=obj if isinstance(obj,list) else [obj]
                for item in items:
                    if isinstance(item,dict) and isinstance(item.get('@graph'),list):items=items+item['@graph']
                for item in items:
                    if not isinstance(item,dict):continue
                    types=item.get('@type',[]);types=[types] if isinstance(types,str) else types
                    if set(types)&{'VideoObject','AudioObject','TVEpisode','Episode','Movie','Article','NewsArticle','BlogPosting'}:
                        date=publication(item)
                        if date:self.dates.append(date|{'publication_source':'JSON-LD '+date['publication_source']})
            except (ValueError,TypeError):pass
            self.buffer=None
    def result(self):
        # Multiple item dates on a listing are not a date for every linked resource.
        unique={d['published_at'] for d in self.dates}
        return self.dates[0] if len(unique)==1 else {}

def page_publication(source):
    parser=PublicationParser();parser.feed(source);parser.close();return parser.result()

def scope_key(site,keyword):
    from urllib.parse import urlsplit
    p=urlsplit(site)
    # Keep path/query: different search scopes on one host must not share a baseline.
    return hashlib.sha256((p.scheme+'://'+p.netloc.lower()+p.path+'?'+p.query+'\n'+' '.join(keyword.casefold().split())).encode()).hexdigest()

class DiscoveryStore:
    def __init__(self,vault):
        self.vault=vault
        with vault.lock:
            vault.db.executescript('''CREATE TABLE IF NOT EXISTS discovery_resources(
 scope TEXT NOT NULL, identity TEXT NOT NULL, url TEXT NOT NULL,title TEXT,
 published_at TEXT,publication_source TEXT,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,last_job_id TEXT,
 PRIMARY KEY(scope,identity));
CREATE INDEX IF NOT EXISTS discovery_resources_seen ON discovery_resources(scope,first_seen);
CREATE TABLE IF NOT EXISTS discovery_pages(scope TEXT NOT NULL,url TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,PRIMARY KEY(scope,url));
CREATE TABLE IF NOT EXISTS discovery_checkpoints(scope TEXT PRIMARY KEY,job_id TEXT,finished_at TEXT,conditions TEXT);
''')
            if not vault.db.execute("SELECT 1 FROM settings WHERE key='discovery_ledger_v1'").fetchone():
                for row in vault.rows('SELECT data FROM discovery_jobs ORDER BY created_at'):
                    job=json.loads(row['data'])
                    for c in job['candidates']:self.observe(job,c,job['created_at'])
                    for page in job.get('pages',[]):
                        if page.get('status')=='ok':self.page_seen(job,page['url'],job['created_at'])
                    if job['status']=='done':self.finish(job,job.get('finished_at',job['created_at']))
                vault.db.execute("INSERT INTO settings VALUES('discovery_ledger_v1','1')")
    def observe(self,job,c,at):
        scope=scope_key(job['site'],job['keyword']);identity=download_identity(c['url'])
        with self.vault.lock:
            self.vault.db.execute('''INSERT INTO discovery_resources VALUES(?,?,?,?,?,?,?,?,?)
ON CONFLICT(scope,identity) DO UPDATE SET last_seen=excluded.last_seen,last_job_id=excluded.last_job_id,
 title=excluded.title,published_at=COALESCE(excluded.published_at,discovery_resources.published_at),
 publication_source=COALESCE(excluded.publication_source,discovery_resources.publication_source)''',
                (scope,identity,c['url'],c.get('title'),c.get('published_at'),c.get('publication_source'),at,at,job['id']))
            row=self.vault.db.execute('SELECT first_seen,published_at,publication_source FROM discovery_resources WHERE scope=? AND identity=?',(scope,identity)).fetchone()
            return dict(row)
    def page_seen(self,job,url,at):
        with self.vault.lock:
            self.vault.db.execute('INSERT INTO discovery_pages VALUES(?,?,?,?) ON CONFLICT(scope,url) DO UPDATE SET last_seen=excluded.last_seen',(scope_key(job['site'],job['keyword']),url,at,at))
    def known_pages(self,job):
        with self.vault.lock:
            return {r[0] for r in self.vault.db.execute('SELECT url FROM discovery_pages WHERE scope=?',(scope_key(job['site'],job['keyword']),))}
    def latest(self,site,keyword):
        with self.vault.lock:
            row=self.vault.db.execute('SELECT * FROM discovery_checkpoints WHERE scope=?',(scope_key(site,keyword),)).fetchone()
            return dict(row) if row else None
    def finish(self,job,at):
        with self.vault.lock:
            self.vault.db.execute('INSERT INTO discovery_checkpoints VALUES(?,?,?,?) ON CONFLICT(scope) DO UPDATE SET job_id=excluded.job_id,finished_at=excluded.finished_at,conditions=excluded.conditions',
                (scope_key(job['site'],job['keyword']),job['id'],at,json.dumps({k:job.get(k) for k in ('site','keyword','template','depth','published_after','include_unknown')})))

import os,stat,json,subprocess,collections,ctypes,struct,datetime
from pathlib import Path
ROOT=Path('/home/nas3/download/.vr')
rows=[];exts=collections.Counter();kinds=collections.Counter();months=collections.Counter();links=0
lib=ctypes.CDLL(None,use_errno=True)
def born(p,s):
 b=ctypes.create_string_buffer(256)
 rc=lib.statx(-100,os.fsencode(p),256,0x800,b) if hasattr(lib,'statx') else lib.syscall(332,-100,ctypes.c_char_p(os.fsencode(p)),256,0x800,b)
 if rc==0 and struct.unpack_from('I',b.raw)[0]&0x800:
  ts=struct.unpack_from('q',b.raw,80)[0]
  if ts>0:return ts,'birthtime'
 return s.st_mtime,'mtime_fallback'
for directory,dirs,files in os.walk(str(ROOT),followlinks=False):
 dirs[:]=[d for d in dirs if not (Path(directory)/d).is_symlink()]
 for name in files:
  p=Path(directory)/name;s=p.lstat()
  if not stat.S_ISREG(s.st_mode):links+=1;continue
  mime=subprocess.check_output(['file','-b','--mime-type','--',str(p)]).decode().strip()
  with p.open('rb') as f:head=f.read(1024)
  video=mime.startswith('video/') or (len(head)>8 and head[4:8]==b'ftyp') or head.startswith(b'\x1aE\xdf\xa3') or (head[:4]==b'RIFF' and head[8:12]==b'AVI ') or (len(head)>376 and head[0]==head[188]==head[376]==0x47)
  ts,basis=born(p,s);month=datetime.datetime.fromtimestamp(ts,datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m')
  row={'path':str(p.relative_to(ROOT)),'size':s.st_size,'dev':s.st_dev,'ino':s.st_ino,'mtime':s.st_mtime_ns,'created':ts,'basis':basis,'month':month,'mime':mime,'video':video}
  rows.append(row);exts[p.suffix.lower()]+=1;kinds['video' if video else 'nonvideo']+=1
  if video:months[month]+=1
report={'source':str(ROOT),'rows':rows,'summary':{'count':len(rows),'bytes':sum(r['size'] for r in rows),'videos':kinds['video'],'nonvideos':kinds['nonvideo'],'video_bytes':sum(r['size'] for r in rows if r['video']),'nonvideo_bytes':sum(r['size'] for r in rows if not r['video']),'extensions':exts,'months':months,'date_basis':dict(collections.Counter(r['basis'] for r in rows)),'links_skipped':links,'possible_duplicate_groups':sum(n>1 for n in collections.Counter(r['size'] for r in rows if r['video']).values())}}
p=Path('/tmp/nova-legacy-audit.json');p.write_text(json.dumps(report,ensure_ascii=False));p.chmod(0o600);print(json.dumps(report['summary'],ensure_ascii=False))

'use strict';
const resourceTypes={all:'全部文件类型',video:'视频',audio:'音频',image:'图片',document:'文档',archive:'压缩包',bt:'磁力 / 种子',other:'其他 / 未知'};
function resourceType(c){
 const mime=(c.content_type||'').toLowerCase().split(';')[0].trim();
 if(mime.startsWith('video/'))return 'video';
 if(mime.startsWith('audio/'))return 'audio';
 if(mime.startsWith('image/'))return 'image';
 if(['hls','dash'].includes(c.kind)||/mpegurl|dash\+xml/.test(mime))return 'video';
 if(['magnet','torrent'].includes(c.kind))return 'bt';
 const classify=value=>{const m=String(value||'').toLowerCase().match(/\.([a-z0-9]+)$/);if(!m)return null;const ext=m[1];
  for(const [type,extensions] of Object.entries({video:'mp4 m4v mkv mov avi wmv webm flv mpg mpeg ts m2ts m3u8 mpd',audio:'mp3 m4a aac wav flac ogg opus wma aiff',image:'jpg jpeg png gif webp avif bmp tif tiff heic heif svg ico',document:'pdf epub mobi azw3 doc docx xls xlsx ppt pptx txt csv md rtf',archive:'zip rar 7z tar gz bz2 xz tgz'}))if(extensions.split(' ').includes(ext))return type;
  return null;};
 try{const u=new URL(c.resolved_url||c.url);const pathType=classify(decodeURIComponent(u.pathname));if(pathType)return pathType;for(const [k,v] of u.searchParams)if(['file','filename','name','download'].includes(k.toLowerCase())){const type=classify(v);if(type)return type}}catch{}
 if(mime==='application/pdf'||mime.startsWith('text/')||/officedocument|msword|epub/.test(mime))return 'document';
 if(/zip|x-rar|x-7z|gzip|x-tar/.test(mime))return 'archive';
 return classify(c.title)||'other';
}
function resourceTypeSelect(id,selected,candidates){return `<label for="${id}">文件类型</label><select id="${id}">${Object.entries(resourceTypes).map(([key,label])=>`<option value="${key}" ${key===selected?'selected':''}>${label}（${key==='all'?candidates.length:candidates.filter(c=>resourceType(c)===key).length}）</option>`).join('')}</select>`}
if(typeof module!=='undefined')module.exports={resourceType};

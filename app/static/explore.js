'use strict';
const explorer={current:null,tab:'original',history:[],historyLoaded:false,historyLoading:false,loading:false,trail:[],cursor:-1,hidden:true,kind:'all',fileType:'all',query:'',historyQuery:'',sniffJob:null,sniffTimer:null,keys:new Map(),queued:new Map(),pending:new Set()};
const linkKinds={hls:'HLS 视频流',dash:'DASH 视频流',media:'媒体直链',magnet:'磁力链接',torrent:'种子链接',direct:'文件直链'};
function renderExplore(){
  if(authStopped)return;saveView();updateNav();const p=explorer.current;
  $('#app').innerHTML=`<div class="page-heading"><div><h1>探索</h1><p class="muted">找到资源，交给 NAS</p></div><button id="explore-more" aria-label="探索更多选项">⋯</button></div><form id="explore-form" class="explore-address"><label class="sr-only" for="explore-url">网址或关键词</label><div class="row"><input id="explore-url" type="text" autocomplete="off" spellcheck="false" placeholder="输入网址或搜索关键词" value="${esc(p?.url||'')}" required><button class="explore-go" ${explorer.loading?'disabled':''}>${explorer.loading?'读取中':'前往'}</button></div></form><div class="explore-tabs"><button class="active">浏览网站</button><button id="auto-explore">关键词搜索</button></div>${explorer.loading?'<p class="muted" role="status">正在读取网页并提取链接…</p>':''}<div id="explore-body"></div>`;
  $('#auto-explore').onclick=()=>navigate('discovery');
  $('#explore-form').onsubmit=e=>{e.preventDefault();const value=$('#explore-url').value.trim();if(/^https?:\/\//i.test(value)||/^[a-z0-9.-]+\.[a-z]{2,}(?:[/:?#]|$)/i.test(value))visitExplore(value);else{discovery.keyword=value;discovery.site=discovery.site||p?.url||'';navigate('discovery');$('#discovery-keyword')?.focus()}};
  $('#explore-more').onclick=()=>{openSheet(`${sheetClose()}<h2>探索选项</h2><div class="menu-list"><button data-explore-choice="history">浏览历史</button><button data-explore-choice="reader" ${p?'':'disabled'}>页面阅读</button><button data-explore-choice="links" ${p?'':'disabled'}>下载链接 ${p?.candidates?.length||0}</button><button id="explore-back" ${explorer.cursor<=0?'disabled':''}>返回上一网页</button>${p?`<a class="outline-link" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">独立打开原站 ↗</a>`:''}</div><p class="muted">原站内跳转不能同步识别；浏览新页面后，将它的网址粘贴回来即可继续提取。</p>`);document.querySelectorAll('[data-explore-choice]').forEach(b=>b.onclick=()=>{$('#modal').close();explorer.tab=b.dataset.exploreChoice;renderExplore()});$('#explore-back').onclick=()=>{$('#modal').close();travelExplore(-1)}};
  renderExploreBody();if(!explorer.historyLoaded&&!explorer.historyLoading)loadExploreHistory();persistMobile();
}
async function loadExploreHistory(){
  explorer.historyLoading=true;
  try{explorer.history=(await api('explore/history')).history;explorer.historyLoaded=true;if(page==='explore'&&explorer.tab==='history')renderExploreBody()}
  catch(e){if(!authStopped)toast(e.message)}finally{explorer.historyLoading=false}
}
async function visitExplore(url,push=true){
  if(explorer.loading)return toast('正在读取网页，请稍候');
  explorer.loading=true;explorer.tab='original';if(page==='explore')renderExplore();
  try{const p=await api('explore/visit',{url:url.trim()});explorer.current=p;explorer.kind='all';explorer.query='';
    if(push){explorer.trail=explorer.trail.slice(0,explorer.cursor+1);explorer.trail.push(p);if(explorer.trail.length>30)explorer.trail.shift();explorer.cursor=explorer.trail.length-1}
    else if(explorer.cursor>=0)explorer.trail[explorer.cursor]=p;
    if(p.status==='ok'&&!p.links.length&&p.candidates.length)explorer.tab='links';
    await loadExploreHistory();restoreExploreSniff(p,true);
  }catch(e){if(!authStopped)toast(e.message)}finally{explorer.loading=false;if(page==='explore'&&!authStopped)renderExplore()}
}
function travelExplore(delta){let i=explorer.cursor+delta;if(i<0||i>=explorer.trail.length)return;explorer.cursor=i;explorer.current=explorer.trail[i];explorer.tab='original';renderExplore()}
function renderExploreBody(){
  const body=$('#explore-body');if(!body||authStopped)return;
  body.classList.remove('focus-browser');
  if(explorer.tab==='history'){renderExploreHistory();return}
  const p=explorer.current;
  if(!p){body.innerHTML='<div class="empty">输入网址开始探索<br><small>可手动点击网页链接继续浏览，下载链接会自动汇总。浏览记录保存在 NAS，保留最近 200 个网址。</small></div>';return}
  if(explorer.tab==='links'){renderExploreLinks();return}
  if(explorer.tab==='original'){
    body.innerHTML=`<div class="embedded-toolbar"><button id="focus-browser" class="small-btn">返回地址栏</button><button id="browser-downloads" aria-expanded="false" aria-controls="browser-results">${browserResourceLabel(p)}</button><a class="outline-link" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">独立打开 ↗</a></div><iframe class="original-site" title="原站浏览" src="${esc(p.url)}" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer" allow="fullscreen" allowfullscreen></iframe><section id="browser-results" class="browser-results" aria-label="下载地址结果" hidden><div class="results-heading"><h2>发现的资源</h2><button id="close-browser-results">收起</button></div><details class="muted results-source"><summary>结果来源：地址栏页面</summary><p>${esc(p.url)}<br>站内跳转无法自动同步；查看新页面的链接，请返回地址栏粘贴该页网址。</p></details><div class="card"><button id="sniff-video" class="accent">继续寻找视频地址</button><details class="sub-options"><summary>嗅探条件</summary><label for="sniff-depth">详情与播放页深度</label><select id="sniff-depth">${[1,2,3,4].map(n=>`<option value="${n}" ${n===2?'selected':''}>${n} 层</option>`).join('')}</select><p class="muted">优先进入播放页；关闭浏览器后 NAS 继续处理。</p></details></div><p id="sniff-status" role="status"></p><div id="browser-results-content"></div></section><p class="muted">网页未显示？<button id="reload-original" class="small-btn">重新加载网页</button> · <a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">独立打开</a> · <button id="reader-fallback" class="small-btn">切换阅读版</button> · 下载候选来自地址栏页面。</p>`;
    body.classList.add('focus-browser');$('#focus-browser').onclick=()=>{body.classList.toggle('focus-browser');$('#focus-browser').textContent=body.classList.contains('focus-browser')?'返回地址栏':'放大浏览'};
    $('#browser-downloads').onclick=()=>{const panel=$('#browser-results');panel.hidden=!panel.hidden;$('#browser-downloads').setAttribute('aria-expanded',String(!panel.hidden));if(!panel.hidden){renderExploreLinks('#browser-results-content');updateExploreSniffStatus();$('#close-browser-results').focus()}};
    $('#sniff-video').onclick=()=>startExploreSniff(Number($('#sniff-depth').value));
    $('#close-browser-results').onclick=()=>{$('#browser-results').hidden=true;$('#browser-downloads').setAttribute('aria-expanded','false');$('#browser-downloads').focus()};
    $('#browser-results').onkeydown=e=>{if(e.key==='Escape')$('#close-browser-results').click()};
    $('#reload-original').onclick=()=>{const frame=document.querySelector('.original-site');frame.src=p.url};
    $('#reader-fallback').onclick=()=>{explorer.tab='reader';renderExplore()};return;
  }
  if(p.status==='error'){body.innerHTML=`<div class="card"><h2>暂时无法读取这个网站</h2><p class="description">${esc(p.error)}</p><a class="outline-link" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">打开原站查看 ↗</a><p class="muted">浏览记录已保存。可以在原站复制实际文件或磁力链接，回“下载”页粘贴。</p></div>`;return}
  body.innerHTML=`<div class="reader-heading"><h2>${esc(p.title)}</h2><p class="muted">${esc(new URL(p.url).hostname)} · ${new Date(p.visited_at).toLocaleString('zh-CN')}<br>已识别 ${p.candidates.length} 个下载候选链接${p.truncated?' · 页面较长，阅读内容已截断':''}</p><button id="view-extracted">查看下载链接 →</button></div><div class="site-reader" id="site-reader" aria-label="网页阅读内容"></div>`;
  // Server renders only a strict allowlist; no remote scripts, styles, images or forms.
  $('#site-reader').innerHTML=p.html;$('#view-extracted').onclick=()=>{explorer.tab='links';renderExplore()};
  $('#site-reader').onclick=e=>{const b=e.target.closest('[data-explore-nav]');if(!b)return;const link=p.links[Number(b.dataset.exploreNav)];if(!link)return;if(link.url.startsWith('magnet:')){explorer.tab='links';explorer.query='';renderExplore();toast('磁力链接已汇总，请选择下载到 NAS')}else visitExplore(link.url)};
}
function renderExploreLinks(target='#explore-body'){
  const p=explorer.current;$(target).innerHTML=`<div class="card">${resourceTypeSelect('explore-file-type',explorer.fileType,p.candidates)}<details><summary>查找与下载选项</summary><label for="explore-search">查找链接</label><input id="explore-search" value="${esc(explorer.query)}" placeholder="文件名、域名或链接关键词"><label><input type="checkbox" id="explore-hidden" ${explorer.hidden?'checked':''}> 下载到隐藏保险箱</label><label for="explore-filter">链接类型</label><select id="explore-filter">${[['all','全部类型'],['magnet','磁力链接'],['torrent','种子链接'],['direct','文件直链']].map(([k,n])=>`<option value="${k}" ${explorer.kind===k?'selected':''}>${n}</option>`).join('')}</select></details></div><div id="explore-candidates"></div>`;
  $('#explore-file-type').onchange=e=>{explorer.fileType=e.target.value;renderExploreCandidates()};
  $('#explore-hidden').onchange=e=>{explorer.hidden=e.target.checked;renderExploreLinks(target)};$('#explore-filter').onchange=e=>{explorer.kind=e.target.value;renderExploreCandidates()};$('#explore-search').oninput=e=>{explorer.query=e.target.value;renderExploreCandidates()};renderExploreCandidates();
}
function renderExploreCandidates(){
  if(!$('#explore-candidates'))return;
  const p=explorer.current;let cs=p.candidates.filter(c=>(explorer.fileType==='all'||resourceType(c)===explorer.fileType)&&(explorer.kind==='all'||c.kind===explorer.kind)&&(c.title+' '+c.url).toLowerCase().includes(explorer.query.toLowerCase()));
  $('#explore-candidates').innerHTML=(cs.length?`<p class="muted">共 ${cs.length} 个候选链接${cs.length>80?'，显示前 80 个，可搜索缩小范围':''}</p>`:'')+cs.slice(0,80).map(c=>{const k=p.id+':'+c.id,done=explorer.queued.has(k),pending=explorer.pending.has(k);return `<div class="card candidate"><span class="pill">${resourceTypes[resourceType(c)]} · ${linkKinds[c.kind]||'链接'}</span><h3>${esc(c.title)}</h3>${c.previous?.length?`<p class="note">${esc(previousDownloadText(c.previous[0]))}${c.previous.length>1?' · 多次记录':''}</p>`:''}<details><summary>链接与更多</summary><p class="candidate-url">${esc(c.url)}</p><button data-candidate-copy="${c.id}">复制链接</button>${c.kind!=='magnet'?`<button class="small-btn" data-candidate-browse="${c.id}">浏览此链接</button>`:''}</details><p class="muted">${c.detail?esc(c.detail):c.kind==='magnet'?'已识别 BT 磁力格式':c.verified?'地址返回文件':'候选链接 · 下载前检查'}</p><div class="actions"><button class="download-candidate" data-candidate-download="${c.id}" ${done||pending||['hls','dash'].includes(c.kind)?'disabled':''}>${['hls','dash'].includes(c.kind)?'播放清单 · 可复制':pending?'正在提交…':done?'已加入下载':'下载到 NAS'}</button></div></div>`}).join('')||'<div class="empty">没有匹配的下载链接<br><small>试试点击页面中的详情链接，或打开原站获取动态、登录后才出现的下载地址。</small></div>';
  document.querySelectorAll('[data-candidate-download]').forEach(b=>b.onclick=()=>downloadExplore(b.dataset.candidateDownload));
  document.querySelectorAll('[data-candidate-copy]').forEach(b=>b.onclick=async()=>{const c=p.candidates.find(c=>c.id===b.dataset.candidateCopy);try{await navigator.clipboard.writeText(c.url);toast('链接已复制')}catch{toast('复制权限不可用，请展开完整链接后长按复制')}});
  document.querySelectorAll('[data-candidate-browse]').forEach(b=>b.onclick=()=>visitExplore(p.candidates.find(c=>c.id===b.dataset.candidateBrowse).url));
}
async function downloadExplore(ident){
  const p=explorer.current,k=p.id+':'+ident;if(explorer.pending.has(k)||explorer.queued.has(k))return;
  if(!explorer.keys.has(k))explorer.keys.set(k,crypto.randomUUID());explorer.pending.add(k);renderExploreCandidates();
  try{const c=p.candidates.find(c=>c.id===ident);const task=await api(c.job_id?'discovery/download':'explore/download',{page_id:p.id,job_id:c.job_id,link_id:ident,hidden:explorer.hidden,request_key:explorer.keys.get(k)});explorer.queued.set(k,task.id);toast(task.status==='submitting'?'任务已保存，正在连接 Aria2':'已加入 NAS 下载，可在“下载”页查看');await refresh()}
  catch(e){if(!authStopped)toast(e.message)}finally{explorer.pending.delete(k);if(page==='explore'&&$('#explore-candidates')&&explorer.current?.id===p.id&&!authStopped)renderExploreCandidates()}
}
function renderExploreHistory(){
  $('#explore-body').innerHTML=`<label for="history-search">搜索浏览历史</label><input id="history-search" value="${esc(explorer.historyQuery)}" placeholder="网站名称或网址"><p class="muted">保存在 NAS · 最近 200 个网址 · 点击重新浏览</p><div id="history-list"></div>`;$('#history-search').oninput=e=>{explorer.historyQuery=e.target.value;renderHistoryList()};renderHistoryList();
}
function renderHistoryList(){
  const q=explorer.historyQuery.toLowerCase();let hs=explorer.history.filter(h=>(h.title+' '+h.url).toLowerCase().includes(q));$('#history-list').innerHTML=hs.map(h=>`<div class="card history-item"><b>${esc(h.title)}</b><p class="candidate-url muted">${esc(h.url)}</p><p class="muted">${new Date(h.visited_at).toLocaleString('zh-CN')} · 浏览 ${h.visit_count} 次${h.status==='error'?' · 上次读取失败':''}</p><div class="actions"><button data-history-open="${h.id}">重新浏览</button><button data-history-snapshot="${h.id}">上次页面</button><button data-history-remove="${h.id}">移除记录</button></div></div>`).join('')||'<div class="empty">暂无匹配的浏览记录。</div>';
  document.querySelectorAll('[data-history-open]').forEach(b=>b.onclick=()=>visitExplore(explorer.history.find(h=>h.id===b.dataset.historyOpen).url));
  document.querySelectorAll('[data-history-snapshot]').forEach(b=>b.onclick=async()=>{try{let p=await api('explore/page?id='+encodeURIComponent(b.dataset.historySnapshot));explorer.current=p;explorer.trail=explorer.trail.slice(0,explorer.cursor+1);explorer.trail.push(p);explorer.cursor=explorer.trail.length-1;explorer.tab='original';renderExplore();restoreExploreSniff(p);toast('已打开上次保存的页面；点击刷新获取最新内容')}catch(e){toast(e.message)}});
  document.querySelectorAll('[data-history-remove]').forEach(b=>b.onclick=()=>{const h=explorer.history.find(h=>h.id===b.dataset.historyRemove),m=$('#modal');m.innerHTML=`<h2>移除浏览记录</h2><p>仅移除“${esc(h.title)}”的历史和页面快照，已创建的下载任务不受影响。</p><div class="actions"><button id="cancel">取消</button><button id="confirm">移除记录</button></div>`;m.showModal();$('#cancel').onclick=()=>m.close();$('#confirm').onclick=async()=>{try{await api('explore/history/delete',{id:h.id});m.close();await loadExploreHistory();toast('浏览记录已移除')}catch(e){toast(e.message)}}});
}

async function startExploreSniff(depth=2){
 const p=explorer.current;if(!p)return;
 try{const j=await api('discovery/start',{site:p.url,keyword:'',browse:true,depth});explorer.sniffJob=j;explorer.fileType='video';pollExploreSniff();updateExploreSniffStatus()}
 catch(e){toast(e.message)}
}
async function restoreExploreSniff(p,auto=false){
 try{const hs=(await api('discovery/history')).jobs;const h=hs.find(j=>j.site===p.url&&!j.keyword);if(explorer.current?.id!==p.id)return;
  if(h){explorer.sniffJob=await api('discovery/job?id='+encodeURIComponent(h.id));explorer.fileType='video';pollExploreSniff()}
  else if(auto&&!hs.some(j=>['running','queued'].includes(j.status)))startExploreSniff(2);
 }catch(e){toast(e.message)}
}
function updateExploreSniffStatus(){
 const j=explorer.sniffJob;if(!j||j.site!==explorer.current?.url)return;
 if($('#sniff-status'))$('#sniff-status').textContent=(discoveryStatus[j.status]||j.status)+' · 视频 '+j.candidates.filter(c=>resourceType(c)==='video').length+' 个 · '+j.pages.length+' 页 · '+j.message;
 if($('#sniff-video'))$('#sniff-video').disabled=['queued','running'].includes(j.status);
}
async function pollExploreSniff(){
 clearTimeout(explorer.sniffTimer);const j=explorer.sniffJob,p=explorer.current;if(!j||!p||j.site!==p.url)return;
 try{const fresh=await api('discovery/job?id='+encodeURIComponent(j.id));if(explorer.current?.id!==p.id)return;explorer.sniffJob=fresh;
  const merged=new Map(p.candidates.map(c=>[c.url,c]));fresh.candidates.forEach(c=>merged.set(c.url,{...c,job_id:fresh.id}));p.candidates=[...merged.values()];
  if(page==='explore'){if($('#browser-downloads'))$('#browser-downloads').textContent=browserResourceLabel(p);if($('#browser-results')&&!$('#browser-results').hidden)renderExploreLinks('#browser-results-content');else if(explorer.tab==='links')renderExploreLinks();updateExploreSniffStatus()}
  if(['queued','running'].includes(fresh.status))explorer.sniffTimer=setTimeout(pollExploreSniff,2200);
 }catch(e){if(!authStopped)explorer.sniffTimer=setTimeout(pollExploreSniff,4000)}
}

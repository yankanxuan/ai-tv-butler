'use strict';
const $=id=>document.getElementById(id);
let token='', activeId='', events=null, config={}, previewUrl='';
const api=async(path,options={})=>{const headers={...(options.headers||{}),'X-Auth-Token':token};if(options.body)headers['Content-Type']='application/json';const r=await fetch(path,{...options,headers,cache:'no-store'});if(r.status===401){logout();throw Error('认证失效，请重新登录');}const data=await r.json();if(!r.ok)throw Error(data.error||`请求失败 ${r.status}`);return data;};
function logout(){token='';activeId='';if(events){events.abort();events=null;}if(previewUrl){URL.revokeObjectURL(previewUrl);previewUrl='';}$('preview').removeAttribute('src');$('preview').hidden=true;$('events').textContent='';$('task-result').textContent='';$('active').classList.add('hidden');$('console').classList.add('hidden');$('login').classList.remove('hidden');$('token').value='';$('model-form').elements.api_key.value='';for(const id of ['history-body','skills-body','schedule-body'])$(id).textContent='';config={};}
$('login-form').onsubmit=async e=>{e.preventDefault();token=$('token').value.trim();try{config=await api('/api/config');$('login').classList.add('hidden');$('console').classList.remove('hidden');initialize();}catch(err){$('login-error').textContent=err.message;token='';}};
$('logout').onclick=logout;
async function watch(id){
  if(events)events.abort();
  if(activeId!==id)$('events').textContent='';
  const controller=new AbortController();events=controller;activeId=id;
  $('active').classList.remove('hidden');$('active-id').textContent=id;
  let reader=null, terminal=false;
  try{
    const response=await fetch('/api/runs/'+encodeURIComponent(id)+'/events',{headers:{'X-Auth-Token':token},signal:controller.signal,cache:'no-store'});
    if(response.status===401){logout();throw Error('认证失效，请重新登录');}
    if(!response.ok){const data=await response.json();throw Error(data.error||'实时连接失败');}
    reader=response.body.getReader();const decoder=new TextDecoder();let buffer='';
    while(true){
      const {value,done}=await reader.read();if(done)break;
      buffer+=decoder.decode(value,{stream:true});buffer=buffer.replace(/\r\n/g,'\n');
      let boundary;
      while((boundary=buffer.indexOf('\n\n'))>=0){
        const frame=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);
        const data=frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
        if(!data)continue;
        const task=JSON.parse(data);renderEvents(task);
        terminal=!['RUNNING','QUEUED','PAUSED'].includes(task.state);
        if(terminal){loadHistory();return;}
      }
    }
    if(!terminal)throw Error('实时连接中断，请点击“恢复实时连接”；也可从任务历史恢复。');
  }catch(error){if(error.name!=='AbortError'&&events===controller){$('task-result').classList.remove('hidden');$('task-result').textContent=error.message;}}
  finally{if(reader){try{await reader.cancel();}catch(_){}reader.releaseLock();}if(events===controller)events=null;}
}
$('reconnect').onclick=()=>{if(activeId)watch(activeId);};
window.addEventListener('pagehide',()=>{if(events)events.abort();if(previewUrl)URL.revokeObjectURL(previewUrl);});
function renderEvents(task){$('events').textContent=(task.events||[]).map(e=>`${new Date(e.at*1000).toLocaleTimeString()}  ${e.kind}  ${JSON.stringify({...e,at:undefined,kind:undefined})}`).join('\n');$('events').scrollTop=$('events').scrollHeight;$('task-result').classList.remove('hidden');$('task-result').textContent=JSON.stringify(task.result||task,null,2);}
$('command-form').onsubmit=async e=>{e.preventDefault();try{const r=await api('/api/runs',{method:'POST',body:JSON.stringify({command:$('command').value})});watch(r.task_id);}catch(err){$('task-result').classList.remove('hidden');$('task-result').textContent=err.message;}};
document.querySelectorAll('[data-control]').forEach(b=>b.onclick=async()=>{try{if(activeId)await api('/api/runs/'+activeId+'/'+b.dataset.control,{method:'POST'});}catch(error){$('task-result').classList.remove('hidden');$('task-result').textContent=error.message;}});
$('refresh-screen').onclick=async()=>{const note=$('screen-note');note.textContent='读取中…';try{const r=await fetch('/api/screenshot',{headers:{'X-Auth-Token':token},cache:'no-store'});if(r.status===401){logout();throw Error('认证失效，请重新登录');}if(!r.ok){const d=await r.json();throw Error(d.error||'截图不可用');}if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=URL.createObjectURL(await r.blob());$('preview').src=previewUrl;$('preview').hidden=false;note.textContent='截图已更新';}catch(err){note.textContent=err.message;$('preview').hidden=true;}};
async function loadHistory(){try{const [runs,memory]=await Promise.all([api('/api/runs'),api('/api/memory')]);$('history-body').innerHTML=runs.tasks.map(t=>`<tr><td>${escapeHtml(t.instruction)} <button data-watch="${escapeHtml(t.id)}">查看 / 恢复</button></td><td>${t.state}</td><td>${t.result?.elapsed??'-'}</td><td>${t.llm_calls}/${t.recoveries}</td><td>${new Date(t.started*1000).toLocaleString()}</td></tr>`).join('')||'<tr><td colspan="5">暂无记录</td></tr>';document.querySelectorAll('[data-watch]').forEach(b=>b.onclick=()=>watch(b.dataset.watch));$('stats').textContent=memory.enabled?`记忆统计：${memory.stats.total_tasks} 条，成功 ${memory.stats.successes}，成功率 ${(memory.stats.success_rate*100).toFixed(1)}%`:'记忆未启用';}catch(e){$('stats').textContent=e.message;}}
async function loadSkills(){const data=await api('/api/skills');$('skills-body').innerHTML=data.skills.map(s=>`<tr><td>${escapeHtml(s.name)}<br><span class="hint">${escapeHtml(s.id)}</span><details><summary>完整步骤、前置 / 成功条件与验证报告</summary><pre>${escapeHtml(JSON.stringify({definition:s.definition,validation:s.validation,approval:s.approval},null,2))}</pre></details></td><td><span class="badge">${s.status}</span></td><td class="actions">${s.status==='draft'?`<button data-skill="${s.id}" data-action="validate">隔离验证</button>`:''}${s.status==='validated'?`<button data-skill="${s.id}" data-action="approve">审批启用</button>`:''}${s.status==='approved'?`<button data-skill="${s.id}" data-action="disable">停用</button>`:''}</td></tr>`).join('')||'<tr><td colspan="3">暂无候选 Skill</td></tr>';document.querySelectorAll('[data-skill]').forEach(b=>b.onclick=async()=>{if(b.dataset.action==='approve'&&!confirm('确认此 Skill 已在隔离模拟设备验证，并允许在匹配条件下执行？'))return;try{await api('/api/skills/'+b.dataset.skill+'/'+b.dataset.action,{method:'POST',body:JSON.stringify({confirm:b.dataset.action==='approve'})});loadSkills();}catch(e){alert(e.message);}});}
async function loadSchedule(){const data=await api('/api/tasks');$('scheduler-status').textContent=data.executor?.message||'独立调度服务在线状态未验证。';$('schedule-body').innerHTML=data.tasks.map(t=>`<tr><td>${escapeHtml(t.command)}<br><span class="hint">${t.kind}</span></td><td>${t.next_run||'-'}</td><td>${t.status}</td><td><button data-remove="${t.id}">删除</button></td></tr>`).join('')||'<tr><td colspan="4">暂无调度任务</td></tr>';document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=async()=>{await api('/api/tasks/'+b.dataset.remove,{method:'DELETE'});loadSchedule();});}
$('schedule-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target),kind=f.get('kind'),value=f.get('when').trim();const body={command:f.get('command'),kind};if(kind==='once')body.when=value;else if(kind==='interval')body.interval=Number(value);else body.cron=value;try{await api('/api/tasks',{method:'POST',body:JSON.stringify(body)});e.target.reset();loadSchedule();}catch(err){alert(err.message);}};
async function loadModel(){const c=await api('/api/config');for(const k of ['provider','base_url','model','vision_model'])$('model-form').elements[k].value=c.llm[k]||'';$('model-form').elements.use_llm.checked=c.agent.use_llm;}
$('model-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target),body=Object.fromEntries(f.entries());body.use_llm=$('model-form').elements.use_llm.checked;try{await api('/api/config',{method:'POST',body:JSON.stringify(body)});$('model-form').elements.api_key.value='';$('model-result').textContent='配置已保存，新任务使用更新后的模型客户端。';}catch(err){$('model-result').textContent=err.message;}};
$('refresh-history').onclick=loadHistory;
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('.tab').forEach(x=>x.classList.add('hidden'));$('tab-'+b.dataset.tab).classList.remove('hidden');if(b.dataset.tab==='skills')loadSkills();if(b.dataset.tab==='schedule')loadSchedule();if(b.dataset.tab==='model')loadModel();});
function escapeHtml(s){const d=document.createElement('div');d.textContent=s??'';return d.innerHTML;}

// ---- V4 语音对话（Alpine.js 组件；与 /api/runs 普通任务互不影响） ----
function voicePanel(){
  // Alpine CSP build: expressions are limited, so derived text lives in state.
  return {
    messages: [], candidates: [], text: '', busy: false, pending: false,
    session: '', sessionLine: '会话：未开始', stateText: '', ttsLine: '', lastReply: '',
    available: true, empty: true, headline: 'V4 Beta', sendLabel: '发送语音对话',
    audio: null,
    init(){
      const self = this;
      const form = document.getElementById('voice-form');
      if (form) form.addEventListener('submit', function(e){ e.preventDefault(); self.send(); });
      const mic = document.getElementById('voice-mic');
      if (mic) mic.addEventListener('click', function(){ self.startMic(); });
      const actions = document.getElementById('voice-actions');
      if (actions) actions.addEventListener('click', function(e){
        const button = e.target.closest && e.target.closest('[data-voice]');
        if (button) self.send(button.dataset.voice);
      });
      const list = document.getElementById('voice-candidates');
      if (list) list.addEventListener('click', function(e){
        const button = e.target.closest && e.target.closest('[data-pick]');
        if (!button) return;
        self.pick(Number(button.dataset.pick));
      });
      window.__voiceRefresh = function(){
        self.available = !!(config.voice && (config.voice.intelligence || {}).enabled);
        self.headline = 'V4 Beta · ' + (self.available ? '已启用' : '需 voice.intelligence.enabled');
        if (!self.available) self.stateText = '语音 Beta 未启用';
        else if (self.stateText === '语音 Beta 未启用') self.stateText = '';
      };
      window.__voiceRefresh();
    },
    addMessage(role, text){
      const prefix = role === 'user' ? '你：' : '管家：';
      this.messages.push({role: role, label: prefix + text});
      this.empty = false;
    },
    async send(value){
      const box = document.getElementById('voice-text');
      const text = (value || (box ? box.value : '') || '').trim();
      if (!text || this.busy) return;
      this.busy = true; this.sendLabel = '处理中…'; if (box) box.value = ''; this.stateText = '处理中…';
      this.addMessage('user', text);
      try {
        const data = await voiceApi('/api/voice', {method: 'POST',
          body: JSON.stringify({text: text, source: 'web_microphone'})});
        this.render(data);
      } catch (err) {
        this.stateText = err.message;
        this.addMessage('agent', '请求失败：' + err.message);
      } finally { this.busy = false; this.sendLabel = '发送语音对话'; }
    },
    render(data){
      this.session = data.session_id || this.session;
      this.pending = data.status === 'NEEDS_CONFIRMATION';
      const rows = data.candidates || [];
      this.candidates = rows.map(function(c, i){
        return {id: c.id, pick: String(i + 1), label: (i + 1) + '. ' + c.title + (c.year ? ' (' + c.year + ')' : '')};
      });
      this.stateText = data.status || '';
      this.sessionLine = '会话 ' + (this.session || '-') + ' · 状态 ' + (data.status || '-') +
        ' · 路由 ' + ((data.decision || {}).route || data.route || '-');
      if (data.reply) { this.lastReply = data.reply; this.addMessage('agent', data.reply); }
      const speech = data.speech || {};
      if (speech.job_id) { this.ttsLine = '语音任务 ' + speech.job_id + '：' + (speech.job_state || 'QUEUED'); this.watchTTS(speech.job_id); }
      else this.ttsLine = speech.reason ? ('未播报：' + speech.reason) : '未启用语音回执';
      const box = document.getElementById('voice-result');
      if (box) { box.classList.remove('hidden');
        box.textContent = JSON.stringify({status: data.status, decision: data.decision, metrics: data.metrics}, null, 2); }
    },
    pick(index){ this.send('第' + index + '部'); },
    confirmAction(){ this.send('确认'); },
    cancelAction(){ this.send('取消'); },
    async watchTTS(jobId){
      for (let i = 0; i < 40; i++) {
        try {
          const job = await voiceApi('/api/voice/tts/' + encodeURIComponent(jobId));
          this.ttsLine = '语音任务 ' + job.id + '：' + job.state + (job.reason ? ('（' + job.reason + '）') : '');
          if (job.state === 'READY' && job.audio_url) { await this.play(job.audio_url); return; }
          if (job.state === 'FAILED') return;
        } catch (err) { this.ttsLine = '语音状态查询失败：' + err.message; return; }
        await new Promise(function(r){ setTimeout(r, 500); });
      }
    },
    async play(audioUrl){
      const name = audioUrl.split('/').pop();
      // Authenticated fetch: never a bare <audio src> without the console token.
      const response = await fetch('/api/voice/audio/' + encodeURIComponent(name),
        {headers: {'X-Auth-Token': token}, cache: 'no-store'});
      if (!response.ok) { this.ttsLine += ' · 音频获取失败 ' + response.status; return; }
      const blob = await response.blob();
      if (this.audio) { this.audio.pause(); URL.revokeObjectURL(this.audio.src); }
      this.audio = new Audio(URL.createObjectURL(blob));
      try { await this.audio.play(); this.ttsLine += ' · 已在本机浏览器播放'; }
      catch (err) { this.ttsLine += ' · 浏览器拦截自动播放，请手动点击'; }
    },
    startMic(){
      const self = this;
      const C = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!C) { this.stateText = '浏览器不支持 Web Speech，可手动输入'; return; }
      const recognition = new C(); recognition.lang = 'zh-CN'; recognition.interimResults = false;
      recognition.onresult = function(e){ const said = e.results[0][0].transcript;
        self.stateText = '已识别：' + said; self.send(said); };
      recognition.onerror = function(e){ self.stateText = '语音不可用：' + e.error; };
      this.stateText = '请说话…';
      try { recognition.start(); } catch (err) { this.stateText = '无法启动识别：' + err.message; }
    }
  };
}
document.addEventListener('alpine:init', function(){ window.Alpine.data('voicePanel', voicePanel); });
async function voiceApi(path, options){
  const headers = Object.assign({'X-Auth-Token': token}, (options && options.headers) || {});
  if (options && options.body) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, Object.assign({}, options, {headers, cache: 'no-store'}));
  if (response.status === 401) { logout(); throw new Error('认证失效，请重新登录'); }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || ('请求失败 ' + response.status));
  return data;
}
function initialize(){if(window.__voiceRefresh)window.__voiceRefresh();api('/api/info').then(d=>$('connection').textContent=`已连接 ${d.model} / ${d.serial}`).catch(e=>$('connection').textContent='设备状态：'+e.message);loadHistory();if(config.voice?.enabled&&('webkitSpeechRecognition' in window||'SpeechRecognition' in window)){const C=window.SpeechRecognition||window.webkitSpeechRecognition,recognition=new C();recognition.lang='zh-CN';recognition.interimResults=false;recognition.onresult=e=>{$('command').value=e.results[0][0].transcript;$('voice-status').textContent='已识别，请检查后发送';};recognition.onerror=e=>$('voice-status').textContent='语音不可用：'+e.error;$('mic').onclick=()=>{recognition.start();$('voice-status').textContent='请说话…';};}else $('voice-status').textContent='语音未启用或浏览器不支持 Web Speech；可在支持的手机浏览器中启用。';}

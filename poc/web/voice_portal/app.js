'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  csrf: '', recorder: null, stream: null, chunks: [], timer: null, started: 0,
  lastRetry: null, works: [], work: null, season: null, episode: null,
  taskId: null, poll: null, selection: null
};

function setStatus(text) { $('status').textContent = text; }
function setError(text) { $('error').textContent = text || ''; }
function show(el, yes) { el.classList.toggle('hidden', !yes); }

function retryable(fn, message) {
  state.lastRetry = fn;
  setError(message);
  show($('retry-box'), true);
}

function clearRetry() {
  state.lastRetry = null;
  show($('retry-box'), false);
  setError('');
}

async function api(path, options = {}, timeout = 20000) {
  const headers = Object.assign({}, options.headers || {});
  if (options.body && !(options.body instanceof Blob)) headers['Content-Type'] = 'application/json';
  if (state.csrf) headers['X-Portal-CSRF'] = state.csrf;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  let response;
  try {
    response = await fetch(path, Object.assign({}, options, {headers, signal: controller.signal, cache: 'no-store'}));
  } catch (err) {
    if (err.name === 'AbortError') throw new Error('请求超时，请检查网络后重试');
    throw new Error('网络中断或无法连接网关，请检查手机与 NAS 的 HTTPS 连接');
  } finally { clearTimeout(timer); }
  let data = null;
  try { data = await response.json(); } catch (_) { data = {error: '响应格式无效'}; }
  if (response.status === 401) {
    show($('app'), false); show($('login'), true);
    throw new Error(data.error || '登录已过期，请重新登录');
  }
  if (!response.ok) {
    const error = new Error(data.error || ('请求失败 ' + response.status));
    error.code = response.status;
    throw error;
  }
  return data;
}

async function login(event) {
  event.preventDefault();
  setError('');
  const token = $('token').value.trim();
  try {
    const data = await api('/api/login', {method: 'POST', body: JSON.stringify({token})});
    state.csrf = data.csrf;
    $('token').value = '';
    await restoreSession();
  } catch (err) { $('login-error').textContent = err.message; }
}

async function restoreSession() {
  try {
    const data = await api('/api/session');
    state.csrf = data.csrf;
    show($('login'), false); show($('app'), true);
    setStatus('已登录，等待录音');
    clearRetry();
  } catch (_) {
    show($('login'), true); show($('app'), false);
  }
}

async function logout() {
  stopTracks();
  try {
    if (state.csrf) await api('/api/logout', {method: 'POST', body: JSON.stringify({})});
  } catch (_) {
    // Local clearing still happens, but the server response is what revokes the session.
  }
  state.csrf = '';
  state.taskId = null;
  show($('app'), false); show($('login'), true);
  setStatus('已退出；服务端会话已撤销');
}

function stopTracks() {
  if (state.stream) state.stream.getTracks().forEach((track) => track.stop());
  state.stream = null;
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  if (state.poll) clearTimeout(state.poll);
  state.poll = null;
}

function chooseMime() {
  const types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
  for (const type of types) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(type)) return type;
  }
  return '';
}

async function startRecording() {
  clearRetry();
  if (!$('consent').checked) {
    retryable(startRecording, '请先勾选录音授权确认；未授权不会打开麦克风。');
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    retryable(startRecording, '浏览器不支持麦克风采集；请使用 HTTPS 和较新的手机浏览器，或导入 WAV。');
    return;
  }
  if (!window.MediaRecorder) {
    retryable(startRecording, '浏览器不支持 MediaRecorder；请升级浏览器或导入 WAV。');
    return;
  }
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true}
    });
    const mimeType = chooseMime();
    state.chunks = [];
    state.recorder = new MediaRecorder(state.stream, mimeType ? {mimeType} : undefined);
    state.recorder.ondataavailable = (event) => { if (event.data.size) state.chunks.push(event.data); };
    state.recorder.onerror = () => { retryable(startRecording, '录音失败，请检查麦克风权限后重试'); stopTracks(); };
    state.recorder.onstop = () => { stopTracks(); convertAndUpload(); };
    state.recorder.start();
    state.started = Date.now();
    setStatus('录音中…再次点击停止');
    $('record').disabled = true;
    $('stop').disabled = false;
    $('timer').textContent = '00:00';
    state.timer = setInterval(() => {
      const seconds = Math.floor((Date.now() - state.started) / 1000);
      if (seconds >= 60) { setStatus('已达到 60 秒上限，正在停止'); stopRecording(); return; }
      $('timer').textContent = String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
    }, 250);
  } catch (err) {
    stopTracks();
    $('record').disabled = false;
    const hint = err && err.name === 'NotAllowedError' ? '麦克风权限被拒绝，请在浏览器设置中允许后重试'
      : err && err.name === 'NotFoundError' ? '没有找到麦克风设备'
      : '无法打开麦克风：' + (err && err.message ? err.message : '未知错误');
    retryable(startRecording, hint + '。手机必须通过 HTTPS 访问。');
  }
}

function stopRecording() {
  if (state.recorder && state.recorder.state !== 'inactive') state.recorder.stop();
  $('record').disabled = false;
  $('stop').disabled = true;
  setStatus('正在转换音频…');
}

async function decodeToWav(arrayBuffer) {
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  const Offline = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  if (!AudioCtx || !Offline) throw new Error('浏览器不支持 Web Audio，无法转换 WAV');
  const context = new AudioCtx();
  let decoded;
  try { decoded = await context.decodeAudioData(arrayBuffer.slice(0)); }
  finally { if (context.close) await context.close(); }
  if (!decoded || !decoded.length) throw new Error('录音为空');
  const duration = Math.min(decoded.duration, 60);
  const frames = Math.max(1, Math.ceil(duration * 16000));
  const offline = new Offline(1, frames, 16000);
  const source = offline.createBufferSource();
  source.buffer = decoded;
  source.connect(offline.destination);
  source.start(0);
  const rendered = await offline.startRendering();
  return encodeWav(rendered.getChannelData(0), 16000);
}

function encodeWav(samples, sampleRate) {
  const bytes = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(bytes);
  let offset = 0;
  const write = (text) => { for (const ch of text) view.setUint8(offset++, ch.charCodeAt(0)); };
  const u32 = (v) => { view.setUint32(offset, v, true); offset += 4; };
  const u16 = (v) => { view.setUint16(offset, v, true); offset += 2; };
  write('RIFF'); u32(36 + samples.length * 2); write('WAVE'); write('fmt '); u32(16);
  u16(1); u16(1); u32(sampleRate); u32(sampleRate * 2); u16(2); u16(16);
  write('data'); u32(samples.length * 2);
  for (const sample of samples) {
    const value = Math.max(-1, Math.min(1, sample));
    view.setInt16(offset, value < 0 ? value * 0x8000 : value * 0x7fff, true);
    offset += 2;
  }
  return new Blob([view], {type: 'audio/wav'});
}

async function convertAndUpload() {
  try {
    const blob = new Blob(state.chunks, {type: state.recorder && state.recorder.mimeType || 'audio/webm'});
    if (!blob.size) throw new Error('没有录到音频');
    setStatus('正在转成 16 kHz WAV…');
    const wav = await decodeToWav(await blob.arrayBuffer());
    await upload(wav, 'push_to_talk_microphone_then_offline');
  } catch (err) {
    retryable(startRecording, err.message || '音频转换失败');
  }
}

async function upload(blob, captureKind) {
  setStatus('正在上传并识别…');
  clearRetry();
  if (!$('consent').checked) {
    retryable(() => upload(blob, captureKind), '请先勾选录音授权确认；未授权不会上传。');
    return;
  }
  try {
    const data = await api('/api/transcribe', {
      method: 'POST', body: blob,
      headers: {'Content-Type': 'audio/wav', 'X-ASR-Consent': 'true', 'X-Capture-Kind': captureKind}
    }, 100000);
    renderASR(data);
  } catch (err) {
    const hint = err.code === 409 ? 'ASR 实验服务正忙，请稍后重试'
      : err.code === 504 ? '识别超时，请重试'
      : err.code === 422 ? '没有识别出文字，请重录或手动输入'
      : err.message;
    retryable(() => upload(blob, captureKind), hint);
    setStatus('识别失败');
  }
}

function renderASR(data) {
  const asr = data.asr || {};
  $('original').textContent = data.original_hypothesis || asr.hypothesis || '';
  $('edited').value = data.original_hypothesis || asr.hypothesis || '';
  show($('result'), true);
  state.selection = null;
  show($('confirm-box'), false);
  show($('task-box'), false);
  state.taskId = null;
  setStatus('识别完成 · ' + (data.capture_kind === 'offline_wav_input' ? '文件回放' : '真人按键录音后离线识别') +
    ' · 音频 ' + (data.audio_seconds || asr.audio_seconds || 0).toFixed(2) + ' 秒');
  renderTitle(data.title || {});
  if (data.current_playback && data.current_playback.title) {
    $('title-status').textContent += ' · 当前播放：' + data.current_playback.title;
  }
}

function renderTitle(title) {
  state.works = title.works || [];
  show($('season-box'), false); show($('episode-box'), false); show($('confirm-box'), false);
  state.work = null; state.season = null; state.episode = null; state.selection = null;
  const box = $('works');
  box.replaceChildren();
  const status = title.status === 'suggested'
    ? '找到真实作品，选择具体季和集后再确认'
    : title.status === 'rejected' ? '这不是明确的片名请求：' + (title.reason || '')
    : '需要补充信息：' + (title.reason || '没有匹配到片库作品');
  $('title-status').textContent = status;
  if (!state.works.length) {
    const p = document.createElement('p');
    p.className = 'hint';
    p.textContent = '没有向执行层提交任何操作。可以修改识别文字后重新匹配，或取消。';
    box.appendChild(p);
    return;
  }
  for (const work of state.works) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'choice';
    const seasons = work.seasons || [];
    button.textContent = work.title + (work.year ? '（' + work.year + '）' : '');
    const small = document.createElement('small');
    small.textContent = seasons.length + ' 季 · ' + work.episode_count + ' 集 · 相似度 ' +
      Number(work.score).toFixed(1) + (work.alias_used ? ' · 审核别名：' + work.alias_used : '');
    button.appendChild(small);
    button.addEventListener('click', () => chooseWork(work, button));
    box.appendChild(button);
  }
}

async function chooseWork(work, button) {
  clearRetry();
  try {
    const data = await api('/api/seasons', {method: 'POST', body: JSON.stringify({work_id: work.work_id})});
    state.work = work; state.season = null; state.episode = null;
    document.querySelectorAll('#works .choice').forEach((b) => b.classList.remove('selected'));
    button.classList.add('selected');
    show($('episode-box'), false); show($('confirm-box'), false);
    renderSeasons(data);
  } catch (err) { retryable(() => chooseWork(work, button), err.message); }
}

function renderSeasons(data) {
  const box = $('seasons'); box.replaceChildren();
  for (const season of data.seasons || []) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'choice';
    button.textContent = '第 ' + season.season + ' 季';
    const small = document.createElement('small');
    small.textContent = season.episode_count + ' 集';
    button.appendChild(small);
    button.addEventListener('click', () => chooseSeason(season, button));
    box.appendChild(button);
  }
  show($('season-box'), true);
  setStatus('已选择作品，请选择季');
}

async function chooseSeason(season, button) {
  clearRetry();
  try {
    const data = await api('/api/episodes', {method: 'POST', body: JSON.stringify({
      work_id: state.work.work_id, season: season.season
    })});
    state.season = season.season; state.episode = null;
    document.querySelectorAll('#seasons .choice').forEach((b) => b.classList.remove('selected'));
    button.classList.add('selected');
    const list = $('episodes'); list.replaceChildren();
    const nums = data.episodes || [];
    for (const episode of nums.slice(0, 80)) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'choice'; b.textContent = '第 ' + episode + ' 集';
      b.addEventListener('click', () => chooseEpisode(episode, b));
      list.appendChild(b);
    }
    $('episode-number').max = String(nums[nums.length - 1] || 1);
    show($('episode-box'), true); show($('confirm-box'), false);
    setStatus('请选择集数（列表最多显示 80 集，也可直接输入）');
  } catch (err) { retryable(() => chooseSeason(season, button), err.message); }
}

async function chooseEpisode(episode, button) {
  clearRetry();
  try {
    const data = await api('/api/select', {method: 'POST', body: JSON.stringify({
      work_id: state.work.work_id, season: state.season, episode: Number(episode)
    })});
    state.episode = Number(episode); state.selection = data;
    document.querySelectorAll('#episodes .choice').forEach((b) => b.classList.remove('selected'));
    if (button) button.classList.add('selected');
    $('selection').textContent = data.command + '。确认后才会提交给电视。';
    show($('confirm-box'), true);
    show($('task-box'), false);
    setStatus('待确认');
  } catch (err) { retryable(() => chooseEpisode(episode, button), err.message); }
}

async function confirmPlay() {
  if (!state.selection) return;
  clearRetry();
  try {
    const data = await api('/api/confirm', {method: 'POST', body: JSON.stringify({confirm: true})}, 30000);
    if (data.status === 'submit_unknown') {
      show($('confirm-box'), false); show($('task-box'), true);
      $('task-state').textContent = '提交结果未知；为避免重复创建任务，未自动重试。幂等键：' +
        (data.idempotency_key || '-');
      setStatus('提交结果未知，需要人工核对');
      return;
    }
    if (data.status !== 'submitted' || !data.task_id) {
      retryable(confirmPlay, '控制台未返回可关联的任务号');
      return;
    }
    state.taskId = data.task_id;
    show($('task-box'), true); show($('confirm-box'), false);
    $('task-state').textContent = '已提交任务 ' + data.task_id + '，等待电视执行…';
    setStatus(data.idempotent ? '已恢复原任务（未重复提交）' : '已在电视执行');
    pollTask();
  } catch (err) {
    retryable(confirmPlay, err.message);
    setStatus('未提交或提交失败');
  }
}

async function cancelAll() {
  clearRetry();
  try {
    const data = await api('/api/cancel', {method: 'POST', body: JSON.stringify({})}, 20000);
    if (data.status === 'cancelled') {
      if (state.poll) clearTimeout(state.poll);
      state.taskId = null; state.selection = null;
      show($('confirm-box'), false); show($('task-box'), false);
      show($('season-box'), false); show($('episode-box'), false);
      setStatus('已取消，最终状态 CANCELLED');
      return;
    }
    if (data.task_id) state.taskId = data.task_id;
    const message = data.status === 'cancel_not_effective'
      ? ('取消未生效：任务最终状态为 ' + (data.final_state || 'UNKNOWN'))
      : ('取消尚未确认；任务号已保留，可安全重试。' + (data.error ? ' ' + data.error : ''));
    retryable(cancelAll, message);
    $('task-state').textContent = message;
    show($('task-box'), true);
    setStatus('取消未确认');
  } catch (err) { retryable(cancelAll, err.message); }
}

async function pollTask() {
  if (!state.taskId) return;
  try {
    const data = await api('/api/task');
    $('task-state').textContent = '任务状态：' + (data.state || 'UNKNOWN');
    if (data.result) $('task-result').textContent = JSON.stringify(data.result, null, 2);
    if (['SUCCESS', 'PARTIAL', 'FAILED', 'UNKNOWN', 'CANCELLED', 'INTERRUPTED'].includes(data.state)) {
      setStatus('任务已结束：' + data.state);
      return;
    }
  } catch (err) { $('task-state').textContent = '状态查询失败：' + err.message; }
  state.poll = setTimeout(pollTask, 1500);
}

async function rematch() {
  clearRetry();
  try {
    const data = await api('/api/retitle', {method: 'POST', body: JSON.stringify({text: $('edited').value})});
    renderTitle(data.title || {});
    setStatus(data.original_hypothesis ? '已用修正文字重新匹配；原始识别仍保留' : '已重新匹配');
  } catch (err) { retryable(rematch, err.message); }
}

function onFile(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  clearRetry();
  if (!$('consent').checked) {
    retryable(() => onFile(event), '请先勾选录音授权确认；未授权不会读取或上传文件。');
    event.target.value = '';
    return;
  }
  setStatus('正在读取 WAV 文件…');
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const wav = await decodeToWav(reader.result);
      await upload(wav, 'offline_wav_input');
    } catch (err) { retryable(() => onFile(event), err.message || '文件转换失败'); }
  };
  reader.onerror = () => retryable(() => onFile(event), '读取文件失败');
  reader.readAsArrayBuffer(file);
}

$('login-form').addEventListener('submit', login);
$('logout').addEventListener('click', logout);
$('record').addEventListener('click', startRecording);
$('stop').addEventListener('click', stopRecording);
$('retry').addEventListener('click', () => { if (state.lastRetry) state.lastRetry(); });
$('rematch').addEventListener('click', rematch);
$('file').addEventListener('change', onFile);
$('pick-episode').addEventListener('click', () => chooseEpisode(Number($('episode-number').value), null));
$('confirm').addEventListener('click', confirmPlay);
$('cancel').addEventListener('click', cancelAll);
$('cancel-task').addEventListener('click', cancelAll);
$('history').addEventListener('click', loadHistory);
async function loadHistory() {
  clearRetry();
  try {
    const data = await api('/api/history');
    const box = $('history-list');
    box.classList.remove('hidden');
    box.textContent = data.count
      ? data.records.map((r) => (r.task_id || '-') + '  ' + (r.task_state || r.state || 'UNKNOWN') +
          '  key=' + (r.key || '-')).join('\n')
      : '没有可访问的历史任务';
    setStatus('已读取 ' + data.count + ' 条历史任务');
  } catch (err) { retryable(loadHistory, err.message); }
}
window.addEventListener('pagehide', stopTracks);
restoreSession();
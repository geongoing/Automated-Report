const $ = id => document.getElementById(id);
const inputCells = ['B32', 'G32', 'D37', 'E37', 'F37', 'F42'];
const qualityCells = ['inlet_ph','inlet_temperature','outlet_ph','outlet_temperature'];
const allManualCells = [...inputCells, ...qualityCells];
const qualityInput = () => Object.fromEntries(qualityCells.map(id=>[id,$(id).value]));
function qualityValueValid(key, value) {
  if (typeof value!=='string' || !value.trim() || value.length>30 || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim())) return false;
  const number=Number(value);
  return Number.isFinite(number) && number>=0 && number<=(key.endsWith('_ph') ? 14 : 1000000);
}
function qualityDraftEdits(draft) {
  const explicit = Object.hasOwn(draft,'quality_edits');
  const entries = explicit ? draft.quality_edits : draft.water_quality;
  if (!entries || typeof entries!=='object') return {};
  return Object.fromEntries(Object.entries(entries).filter(([key,value])=>qualityCells.includes(key) &&
    ((explicit && value==='') || qualityValueValid(key,value))));
}
function seedQuality() {
  state.qualityEdits={};
  const profile=state.hmiProfile;
  for (const key of qualityCells) $(key).value=profile?.water_quality?.[key] || '';
  $('quality-source').textContent=profile?.previous_name ? `전날 HMI (${profile.previous_date}) 기준 · 금일 측정값을 확인·수정하세요.` : '전날 HMI 측정값이 없습니다. 금일 측정값을 입력하세요.';
}

function hmiDateLabel(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return '';
  const date = new Date(value + 'T00:00:00Z');
  if (Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== value) return '';
  return `${value}(${'일월화수목금토'[date.getUTCDay()]})`;
}
function refreshHmiDate(input) {
  if (input.type !== 'date') return;
  const label = hmiDateLabel(input.value);
  input.parentElement.querySelector('.hmi-date-value').textContent = label || '날짜 선택';
  input.setAttribute('aria-description', label || '날짜 미입력');
}
const state = { targetDate: null, dutyManual: false, automatic: null, files: [], folder: null, reportFolders: null, hmiFolder: null, followingLatest: true, inspected: null, preview: null, revision: 0, fileRevision: 0, busy: false, scanning: false, savingSettings: false, stopped: false, tab: 'entry', hmiAvailable: false, hmiProfile: null, hmiLoading: false, hmiRevision: 0, hmiDirty: false, hmiEdits: {}, qualityEdits: {}, needHmiRefresh: false, draftDate: null, draftReady: false, draftLoading: false, draftScope: '', draftStamp: 0 };
const fmt = (value, digits = 3) => Number(value).toLocaleString('ko-KR', { maximumFractionDigits: digits });
const settingsKey = data => JSON.stringify([data.raw_folder, data.final_folder, data.hmi_folder]);
let errorTimer, draftTimer, draftSaving = null;
const pendingDrafts = new Map();
function draftStatus(text) { if ($('draft-status').textContent !== text) $('draft-status').textContent = text; }
function draftKey(date) { return `songdo:draft:v1:${encodeURIComponent(state.draftScope)}:${date}`; }
function cachedDraft(date) {
  try {
   let latest=null;
   const key=draftKey(date);
   for (const candidate of new Set([key,`songdo:draft:v1:${encodeURIComponent(location.origin)}:${date}`])) {
    let draft;
    try { draft=JSON.parse(localStorage.getItem(candidate) || 'null'); }
    catch { continue; }
    if (!draft || (candidate!==key && draft.hmi_folder!==state.hmiFolder) || draft.record_date !== date || !Number.isSafeInteger(draft.updated_at) || !draft.values ||
        !inputCells.every(cell => typeof draft.values[cell] === 'string') ||
        !Array.isArray(draft.day_workers) || draft.day_workers.length !== 8 ||
        !Array.isArray(draft.night_workers) || draft.night_workers.length !== 2 ||
        ![...draft.day_workers,...draft.night_workers].every(name=>typeof name==='string') ||
        !draft.hmi_fields || typeof draft.hmi_fields !== 'object' ||
        (draft.water_quality && !qualityCells.every(key=>typeof draft.water_quality[key]==='string'))) continue;
    if (!latest || draft.updated_at>latest.updated_at) latest=draft;
   }
   return latest;
  } catch { return null; }
}
function cacheDraft(draft) {
  try { localStorage.setItem(draftKey(draft.record_date),JSON.stringify(draft)); return true; }
  catch { return false; }
}
function queueDraft() {
  if (!state.draftReady || state.draftLoading || !state.draftDate) return;
  state.draftStamp = Math.max(Date.now(),state.draftStamp + 1);
  const draft = {record_date:state.draftDate, updated_at:state.draftStamp,
    values:Object.fromEntries(inputCells.map(cell=>[cell,$(cell).value])),
    day_workers:Array.from(document.querySelectorAll('#day-worker-fields input')).map(input=>input.value),
    night_workers:Array.from(document.querySelectorAll('#night-worker-fields input')).map(input=>input.value),
    water_quality:qualityInput(), quality_edits:{...state.qualityEdits}, hmi_fields:{...state.hmiEdits}, hmi_folder:state.hmiFolder || ''};
  cacheDraft(draft);
  pendingDrafts.set(draft.record_date,draft);
  draftStatus('임시 저장 중…');
  clearTimeout(draftTimer); draftTimer=setTimeout(flushDrafts,300);
}
function flushDrafts() {
  clearTimeout(draftTimer);
  if (draftSaving) return draftSaving;
  if (!pendingDrafts.size) return Promise.resolve();
  draftSaving=(async()=>{
   try {
    while (pendingDrafts.size) {
      const [date,draft]=pendingDrafts.entries().next().value;
      try {
        const result=await api('/api/draft',draft);
        state.draftStamp=Math.max(state.draftStamp,result.draft.updated_at);
        if (pendingDrafts.get(date) === draft) {
          pendingDrafts.delete(date);
          if (date===state.draftDate && !state.draftLoading) draftStatus('임시 저장됨');
        }
      } catch {
        if (date===state.draftDate && !state.draftLoading) draftStatus(cachedDraft(date) ? '브라우저에 임시 저장됨' : '임시 저장 실패');
        return;
      }
    }
   } finally { draftSaving=null; }
  })();
  return draftSaving;
}
function flushDraftBeacons() {
  clearTimeout(draftTimer);
  for (const draft of pendingDrafts.values()) {
    try { navigator.sendBeacon('/api/draft',new Blob([JSON.stringify(draft)],{type:'application/json'})); }
    catch { /* The synchronous browser cache remains available. */ }
  }
}
async function restoreDraft(date, revision) {
  const local=cachedDraft(date);
  let remote=null, failed=false;
  try { remote=(await api('/api/draft?date='+encodeURIComponent(date))).draft; }
  catch { failed=true; }
  if (revision!==state.fileRevision) return;
  const draft=!remote || (local && local.updated_at>remote.updated_at) ? local : remote;
  state.draftDate=date; seedQuality();
  if (!draft) { draftStatus(failed ? '임시 저장 확인 실패' : ''); return; }
  state.draftStamp=Math.max(state.draftStamp,draft.updated_at);
  for (const cell of inputCells) $(cell).value=draft.values[cell];
  if (draft.hmi_folder===state.hmiFolder) {
    state.qualityEdits=qualityDraftEdits(draft);
    for (const [key,value] of Object.entries(state.qualityEdits)) $(key).value=value;
  }
  document.querySelectorAll('#day-worker-fields input').forEach((input,i)=>input.value=draft.day_workers[i]);
  document.querySelectorAll('#night-worker-fields input').forEach((input,i)=>input.value=draft.night_workers[i]);
  if (draft.hmi_folder===state.hmiFolder) {
    state.hmiEdits={};
    for (const [cell,value] of Object.entries(draft.hmi_fields)) {
      if (!hmiEditableCells.includes(cell) || typeof value!=='string') continue;
      const element=$(`hmi-${cell}`);
      if (cell.endsWith('28') && !Array.from(element.options).some(option=>option.value===value)) element.add(new Option(value,value));
      element.value=value; refreshHmiDate(element); state.hmiEdits[cell]=value;
    }
  }
  state.hmiDirty=Object.keys(state.hmiEdits).length>0 || Object.keys(state.qualityEdits).length>0;
  cacheDraft(draft);
  if (!remote || draft.updated_at>remote.updated_at) {
    pendingDrafts.set(date,draft); clearTimeout(draftTimer); draftTimer=setTimeout(flushDrafts,0);
  }
  draftStatus('임시 저장 복원됨');
}
function showError(message) {
  $('error-message').textContent = message; $('error-message').hidden = false;
  clearTimeout(errorTimer); errorTimer = setTimeout(() => $('error-message').hidden = true, 15000);
}
function countInputs() { return inputCells.filter(id => $(id).value.trim() !== '' && $(id).validity.valid).length; }
function updateButtons() {
  if (state.stopped) {
    document.querySelectorAll('input,select,textarea,button').forEach(c => c.disabled = true);
    return;
  }
  const locked = state.busy || state.scanning || state.hmiLoading || state.savingSettings || state.pickingFolder || state.draftLoading;
  for (const id of ['server-stop', 'refresh', 'raw-select', 'folder-save', 'hmi-profile-reload', 'duty-date']) $(id).disabled = locked;
  for (const id of ['entry-tab', 'settings-tab', 'back-to-entry', 'source-settings']) $(id).disabled = state.busy || state.savingSettings || state.pickingFolder;
  document.querySelectorAll('.folder-browse,#folder-form input').forEach(control=>control.disabled=Boolean(state.busy || state.savingSettings || state.pickingFolder));
  $('entry-tab').disabled ||= !state.configured;
  $('recovery-retry').disabled = locked || state.recoveryLoading || state.recoveryBusy;
  document.querySelectorAll('.open-output-folder').forEach(button => button.disabled = state.busy);
  const count = countInputs();
  $('preview-button').disabled = locked || !state.inspected || count !== inputCells.length;
  const workersReady = !state.hmiLoading && hmiWorkersValid();
  const hmiDate = state.hmiProfile?.record_date;
  $('generate-button').disabled = locked || !state.inspected || count !== inputCells.length || !state.hmiAvailable || !workersReady || !qualityCells.every(id=>$(id).value.trim()!=='' && $(id).validity.valid) || hmiDate !== state.inspected?.record_date;
  if (!state.busy) $('generate-button').textContent = state.existing?.outputs?.length ? '재작성 →' : state.preview?.warnings?.length ? '확인한 값으로 생성 →' : '일지 생성 →';
  document.querySelectorAll('#report-form input,#report-form select').forEach(control=>control.disabled=state.busy || state.draftLoading || state.hmiLoading || !state.inspected);
}
function invalidatePreview() {
  state.preview = null; $('success').hidden = true;
  $('metrics').replaceChildren(); $('calculation').hidden = true;
  renderWarnings(state.inspected?.warnings || []); updateButtons();
}
function spec(kind) { return { id: $(`${kind}-select`).value }; }
function payload(withInputs = false) {
  const data = { record_date: state.targetDate, raw: spec('raw'), previous: spec('previous') };
  if (withInputs) data.inputs = Object.fromEntries(inputCells.map(id => [id, $(id).value]));
  return data;
}
async function api(path, data) {
  const response = await fetch(path, data ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data), ...(path==='/api/draft' ? {keepalive:true} : {}) } : {});
  const body = await response.json();
  if (!response.ok) { const error=Error(body.error || '요청을 처리하지 못했습니다.'); error.data=body; throw error; }
  return body;
}
async function loadFiles(initial = false, background = false) {
  if (state.scanning) return false;
  state.scanning = true;
  try {
  const old = { raw: $('raw-select').value, previous: $('previous-select').value };
  const oldSignature = JSON.stringify(state.files.filter(f => f.id === old.raw || f.id === old.previous));
  const data = await api('/api/files' + (state.dutyManual && state.targetDate ? '?date='+encodeURIComponent(state.targetDate) : ''));
  if (background && (state.busy || state.hmiDirty || dayWorkers().length || nightWorkers().some(Boolean) || allManualCells.some(id => $(id).value !== ''))) return false;
  const folderChanged = state.folder !== settingsKey(data);
  const reportFolderChanged = state.reportFolders !== JSON.stringify([data.raw_folder, data.final_folder]);
  const dutyChanged = state.targetDate !== data.duty.record_date;
  state.targetDate = data.duty.record_date; state.automatic = data.automatic;
  $('duty-date').value = state.targetDate; refreshHmiDate($('duty-date'));
  $('duty-report-hint').textContent = `당직일 ${state.targetDate} · 필요한 당일 레포트 ${data.duty.expected_raw_name} (${data.duty.report_file_date} 저장)`;
  showSettings(data); state.files = data.files;
  for (const kind of ['raw', 'previous']) {
    const select = $(`${kind}-select`); select.replaceChildren(new Option(kind === 'raw' ? '당일 레포트 없음' : '전날 최종 일지 없음', ''));
    for (const file of data.files) {
      if (kind === 'raw' && (file.kind !== 'raw' || !data.automatic.raw_ids.includes(file.id))) continue;
      if (kind === 'previous' && file.kind !== 'final') continue;
      select.add(new Option(`${file.record_date} 기록 · ${file.relative_path}`, file.id));
    }
    if (Array.from(select.options).some(x => x.value === old[kind])) select.value = old[kind];
  }
  if (initial || folderChanged || dutyChanged || !$('raw-select').value) {
    $('raw-select').value = data.automatic.raw_id || '';
  }
  suggestPrevious($('raw-select').value);
  const changed = folderChanged || dutyChanged || old.raw !== $('raw-select').value || old.previous !== $('previous-select').value || oldSignature !== JSON.stringify(state.files.filter(f => f.id === $('raw-select').value || f.id === $('previous-select').value));
  if (reportFolderChanged || dutyChanged || old.raw !== $('raw-select').value) { for (const id of allManualCells) $(id).value = ''; }
  if (changed) { ++state.fileRevision; ++state.revision; clearInspect(); }
  updateButtons();
  return changed;
  } finally { state.scanning = false; updateButtons(); }
}
function showSettings(data) {
  showUpdate(data.update);
  $('app-version').textContent=data.version ? 'v'+data.version : '';
  state.draftScope=data.draft_scope || location.origin;
  state.configured=Boolean(data.configured);
  state.configurationMessage=data.configuration_message || '';
  $('setup-message').hidden=state.configured;
  $('setup-message').textContent=state.configurationMessage;
  $('source-details').hidden=!state.configured;
  $('back-to-entry').hidden=!state.configured;
  if (state.folder !== settingsKey(data)) {
    $('folder-path').value = data.raw_folder;
    $('final-folder-path').value = data.final_folder;
    $('hmi-folder-path').value = data.hmi_folder;
  }
  if (state.hmiFolder !== data.hmi_folder) state.needHmiRefresh = true;
  state.hmiFolder = data.hmi_folder;
  state.reportFolders = JSON.stringify([data.raw_folder, data.final_folder]);
  state.folder = settingsKey(data);
  $('output-location').textContent = state.configured ? `최종 일지 저장: ${data.output_folder} / HMI 저장: ${data.hmi_folder}` : '';
  state.hmiAvailable = data.hmi_available;
  $('hmi-template-info').textContent = data.hmi_available ? `HMI 양식: ${data.hmi_template_label || data.hmi_template}` : 'HMI 양식을 찾을 수 없습니다.';
}
function showUpdate(update) {
  if (!update) return;
  $('update-version').textContent='v'+update.current_version;
  $('update-message').textContent=update.message;
  $('update-notes').href=update.release_url;
}
$('update-check').addEventListener('click',async()=>{
  if (state.stopped || state.busy || state.checkingUpdate) return;
  state.checkingUpdate=true; $('update-check').disabled=true;
  $('update-message').textContent='새 정식 버전을 확인하고 있습니다…';
  try { showUpdate(await api('/api/update/check',{})); }
  catch { $('update-message').textContent='업데이트를 확인하지 못했습니다. 현재 버전으로 계속 사용할 수 있습니다.'; }
  finally { state.checkingUpdate=false; $('update-check').disabled=state.stopped || state.busy; }
});
function suggestPrevious(rawId) {
  $('previous-select').value = '';
  const raw = state.files.find(f => f.id === rawId);
  if (!raw) { $('raw-hint').textContent = state.automatic?.message || '당일 레포트 폴더에 파일을 넣어 주세요.'; $('previous-hint').textContent = '당일 레포트를 찾으면 전날 최종 일지를 연결합니다.'; return; }
  const date = new Date(raw.record_date + 'T00:00:00Z');
  date.setUTCDate(date.getUTCDate()-1); const expected = date.toISOString().slice(0,10);
  const candidates = state.files.filter(f => f.kind === 'final' && f.record_date === expected);
  if (candidates[0]) $('previous-select').value = candidates[0].id;
  $('raw-hint').textContent = `당직일 ${raw.record_date} · ${raw.name}`;
  $('previous-hint').textContent = candidates[0] ? `운영 기록일 ${expected} · 전날 최종 일지 자동 연결` : `기록일 ${expected}의 최종 일지를 과거 레포트 폴더에 넣어 주세요.`;
}
function renderWarnings(warnings) {
  $('warnings').replaceChildren(); $('warnings').hidden = !warnings.length;
  for (const text of warnings) { const p = document.createElement('p'); p.textContent = text; $('warnings').append(p); }
}
function renderInspect(data) {
  state.existing=data.existing || null;
  $('record-subtitle').textContent = '운영 기록일 · ' + data.output_name;
  $('carry-body').replaceChildren();
  for (const row of data.carry) {
    const tr = document.createElement('tr');
    for (const value of [row.label, `${row.source} → ${row.cell}`, fmt(row.value)]) { const td = document.createElement('td'); td.textContent = value; tr.append(td); }
    $('carry-body').append(tr);
  }
  const water = data.carry.find(r => r.cell === 'F41').value;
  $('water-hint').textContent = `시상수 전일지침 ${fmt(water)} ㎥`;
  $('F42').min = water;
  $('file-status').textContent = '당일 레포트 · 전날 최종 일지 연결 완료';
  $('file-status').classList.add('ok'); renderWarnings(data.warnings); updateButtons();
}
function clearInspect() {
  state.existing=null;
  state.inspected = null;
  state.draftReady = false;
  $('record-subtitle').textContent = '운영 기록 날짜를 확인해 주세요.';
  const tr = document.createElement('tr'), td = document.createElement('td'); td.colSpan = 3; td.className = 'empty-state'; td.textContent = '두 파일을 불러오면 이월값이 표시됩니다.'; tr.append(td); $('carry-body').replaceChildren(tr);
  $('water-hint').textContent = '전일지침을 불러오면 함께 표시합니다.'; $('F42').min = '0';
  $('quality-source').textContent='';
  $('file-status').classList.remove('ok', 'missing-report'); invalidatePreview();
}
async function inspectFiles() {
  const rev = ++state.fileRevision; ++state.revision; state.draftLoading=true; clearInspect(); draftStatus('');
  try {
    if (!state.configured) {
      state.draftDate=null;
      $('file-status').textContent=state.configurationMessage;
      switchTab('settings');
      return;
    }
    if (!spec('raw').id || !spec('previous').id) {
      state.draftDate=null; draftStatus('');
      $('file-status').textContent = !spec('raw').id ? state.automatic?.message || '당일 레포트 폴더에서 파일을 찾지 못했습니다.' : $('previous-hint').textContent;
      $('file-status').classList.add('missing-report'); return;
    }
    $('file-status').textContent = '파일의 날짜와 전날 수치를 확인하고 있습니다…';
    const data = await api('/api/inspect', payload()); if (rev !== state.fileRevision) return;
    state.inspected = data; renderInspect(data); await ensureHmiProfile(data.record_date);
    if (rev!==state.fileRevision) return;
    await restoreDraft(data.record_date,rev);
  }
  catch (e) { if (rev !== state.fileRevision) return; $('file-status').textContent = e.message; showError(e.message); }
  finally { if (rev===state.fileRevision) { state.draftLoading=false; state.draftReady=Boolean(state.inspected); updateButtons(); } }
}
function renderMetrics(data) {
  $('calculation').hidden = false;
  $('metrics').replaceChildren();
  for (const metric of data.metrics) {
    const row = document.createElement('div'); row.className = 'metric';
    const label = document.createElement('span'); label.textContent = metric.label;
    const value = document.createElement('strong'); value.textContent = fmt(metric.value, metric.unit === 'TON' ? 2 : 3);
    const unit = document.createElement('small'); unit.textContent = metric.unit; value.append(unit); row.append(label,value); $('metrics').append(row);
  }
  renderWarnings(data.warnings);
}
function renderOutputs(data) {
  state.existing=data.existing || null;
  const outputs = data.outputs || [];
  $('success').querySelector('h3').textContent = '일지 2개가 완성됐습니다.';
  $('saved-name').textContent = `${data.record_date} 운영 기록`;
  $('output-downloads').replaceChildren();
  for (const output of outputs) {
    const a = document.createElement('a'); a.className = 'download-button'; a.href = output.download_url;
    a.textContent = (output.kind === 'hmi' ? '송도 HMI 다운로드 ↓' : '최종 일지 다운로드 ↓');
    const group = document.createElement('div'); group.className = 'output-actions';
    const button = document.createElement('button'); button.type = 'button';
    button.className = 'text-button open-output-folder';
    button.textContent = output.kind === 'hmi' ? 'HMI 폴더 열기 ↗' : '최종 일지 폴더 열기 ↗';
    button.addEventListener('click', async () => {
      if (state.busy || state.stopped) return;
      button.disabled = true;
      try { await api('/api/open-output-folder', {kind: output.kind, folder: output.folder}); }
      catch (e) { showError(e.message); }
      finally { button.disabled = state.busy || state.stopped; }
    });
    group.append(a, button); $('output-downloads').append(group);
  }
  $('saved-path').textContent = outputs.map(o => o.path).join('\n');
  $('hmi-completion-note').textContent = data.hmi?.note || data.note || '';
  $('success').hidden = false;
}
function existingLinks(target, outputs) {
  target.replaceChildren();
  for (const output of outputs) {
    const li=document.createElement('li'), link=document.createElement('a');
    link.href=output.download_url;
    link.textContent=`${output.kind==='hmi' ? 'HMI' : '최종 일지'} · ${output.name}`;
    li.append(link); target.append(li);
  }
}
async function confirmRewrite(existing) {
  if (!existing?.outputs?.length) return '';
  const dialog=$('rewrite-dialog');
  $('rewrite-date').textContent=`운영 기록일 ${existing.record_date}`;
  existingLinks($('rewrite-files'),existing.outputs);
  dialog.returnValue='';
  const answer=new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='rewrite'),{once:true}));
  dialog.showModal(); $('rewrite-cancel').focus();
  return await answer ? existing.token : null;
}
$('rewrite-cancel').addEventListener('click',()=>$('rewrite-dialog').close('cancel'));
$('rewrite-confirm').addEventListener('click',()=>$('rewrite-dialog').close('rewrite'));
for (const button of document.querySelectorAll('.folder-browse')) button.addEventListener('click', async()=>{
  if (state.busy || state.savingSettings || state.pickingFolder || state.stopped) return;
  const input=$(button.dataset.input);
  state.pickingFolder=true; updateButtons(); $('folder-message').textContent='폴더 선택 창에서 경로를 선택해 주세요.';
  try {
    const result=await api('/api/select-folder',{kind:button.dataset.kind,initial:input.value});
    if (result.path!==null) { input.value=result.path; $('folder-message').textContent='선택한 경로를 적용하려면 경로 저장을 누르세요.'; }
    else $('folder-message').textContent='';
  } catch (e) { $('folder-message').textContent=e.message; showError(e.message); }
  finally { state.pickingFolder=false; updateButtons(); button.focus(); }
});
$('duty-date').addEventListener('change', async () => {
  const date = $('duty-date').value;
  refreshHmiDate($('duty-date'));
  if (state.busy || state.scanning) return;
  if (!date) {
    state.targetDate=null; state.dutyManual=true; ++state.fileRevision; ++state.revision; clearInspect();
    $('file-status').textContent='작성할 당직일을 선택해 주세요.'; return;
  }
  queueDraft(); state.draftLoading=true; updateButtons(); await flushDrafts();
  state.targetDate = date; state.dutyManual = true; state.followingLatest = true;
  ++state.fileRevision; ++state.revision; clearInspect();
  for (const id of allManualCells) $(id).value = '';
  document.querySelectorAll('#day-worker-fields input,#night-worker-fields input').forEach(input=>input.value='');
  state.hmiProfile=null; state.hmiEdits={}; state.qualityEdits={}; state.hmiDirty=false;
  document.querySelectorAll('#hmi-carry-body input,#hmi-carry-body select').forEach(input=>{input.value='';refreshHmiDate(input);});
  $('carry-note').textContent='당직일 자료 확인 중'; $('hmi-profile-info').textContent='';
  try { await loadFiles(true); await inspectFiles(); }
  catch (e) { $('file-status').textContent=e.message; showError(e.message); }
  finally { state.draftLoading=false; updateButtons(); }
});
$('duty-date').addEventListener('click',()=>{
  if (!$('duty-date').disabled && typeof $('duty-date').showPicker==='function') {
    try { $('duty-date').showPicker(); } catch { /* Keep the native date control available. */ }
  }
});
$('raw-select').addEventListener('change', async () => {
  state.followingLatest = false;
  for (const id of allManualCells) $(id).value = '';
  suggestPrevious($('raw-select').value); await inspectFiles();
});
for (const id of inputCells) $(id).addEventListener('input', () => { ++state.revision; invalidatePreview(); queueDraft(); });
$('refresh').addEventListener('click', async () => {
  try { await loadFiles(state.followingLatest); await inspectFiles(); }
  catch (e) { ++state.fileRevision; ++state.revision; clearInspect(); $('file-status').textContent = e.message; showError(e.message); }
});
$('folder-form').addEventListener('submit', async e => {
  e.preventDefault(); if (state.busy || state.scanning || state.savingSettings || state.stopped) return;
  state.savingSettings = true; updateButtons(); $('folder-message').textContent = '경로를 확인하고 있습니다…';
  const firstSetup=!state.configured;
  try {
    await api('/api/settings', { raw_folder: $('folder-path').value, final_folder: $('final-folder-path').value, hmi_folder: $('hmi-folder-path').value });
    state.followingLatest = true;
    await loadFiles(true); await inspectFiles();
    $('folder-message').textContent = '세 경로를 저장했습니다. 다음 실행에도 그대로 사용합니다.';
    if (firstSetup) switchTab('entry');
  } catch (e) { $('folder-message').textContent = e.message; showError(e.message); }
  finally { state.savingSettings = false; updateButtons(); }
});
$('preview-button').addEventListener('click', async () => {
  if (state.busy || state.stopped) return;
  if (!state.inspected || countInputs() !== inputCells.length) { $('report-form').reportValidity(); return; }
  const rev = ++state.revision; state.busy = true; updateButtons(); $('preview-button').textContent = '계산 중…';
  try { const data = await api('/api/preview', payload(true)); if (rev !== state.revision) return; state.preview = data; renderMetrics(data); state.existing=data.existing || null; }
  catch (e) { state.preview = null; showError(e.message); }
  finally { state.busy = false; $('preview-button').textContent = '계산 확인'; updateButtons(); }
});
$('report-form').addEventListener('submit', async e => {
  e.preventDefault();
  if ($('generate-button').disabled || state.busy || state.stopped) return;
  queueDraft(); flushDrafts();
  const rev = state.revision; state.busy = true; updateButtons(); $('generate-button').textContent = '일지를 만들고 있습니다…';
  // Freeze source and input controls while the output is being written.
  const controls = [...document.querySelectorAll('input,select,textarea')]; controls.forEach(c => c.disabled = true);
  try {
    const request = { ...payload(true), hmi: hmiInput() };
    const preview = await api('/api/preview', request);
    if (rev !== state.revision) return;
    const warningsReviewed = state.preview?.source_token === preview.source_token && JSON.stringify(state.preview?.metrics) === JSON.stringify(preview.metrics);
    state.preview = preview; renderMetrics(preview);
    state.existing=preview.existing || null;
    // A negative computed total needs review before files are written.
    if (preview.warnings.length && !warningsReviewed) { showError('계산 결과에 음수가 있습니다. 값을 확인한 뒤 생성 버튼을 다시 눌러 주세요.'); return; }
    const rewriteToken=await confirmRewrite(preview.existing);
    if (rewriteToken===null || rev!==state.revision) return;
    const data = await api('/api/generate', { ...request, source_token: preview.source_token, rewrite_token:rewriteToken });
    if (rev !== state.revision) return;
    renderOutputs(data); renderMetrics(data);
    await loadFiles(); $('success').scrollIntoView({ behavior:'smooth',block:'nearest' });
  } catch (e) { if (e.data?.code==='duplicate_report') state.existing=e.data.existing || null; showError(e.message); }
  finally { controls.forEach(c => c.disabled = false); $('previous-select').disabled = true; state.busy = false; $('generate-button').textContent = '일지 생성 →'; updateButtons(); refreshRecovery(); }
});
function dayWorkers() { return Array.from(document.querySelectorAll('#day-worker-fields input')).map(i => i.value.trim()).filter(Boolean); }
function nightWorkers() { return Array.from(document.querySelectorAll('#night-worker-fields input')).map(i => i.value.trim()); }
function hmiWorkersValid() { const names = nightWorkers(); return names.length === 2 && names.every(n => n.length > 0 && n.length <= 30) && dayWorkers().length <= 8; }
const hmiEditableCells = [...'CDEFGHIJ'].flatMap(c => [`${c}28`,`${c}32`,`${c}33`,`${c}34`]);
function hmiInput() {
  const fields = Object.fromEntries(hmiEditableCells.map(c => [c, $(`hmi-${c}`).value]));
  return { water_quality: qualityInput(), record_date: state.hmiProfile?.record_date, seed_token: state.hmiProfile?.seed_token,
           day_workers: dayWorkers(), night_workers: nightWorkers(), fields };
}
function hmiEdited(event) {
  if (event?.target) refreshHmiDate(event.target);
  state.hmiDirty = true; $('success').hidden = true;
  const cell=event?.target.id?.replace(/^hmi-/,'');
  if (qualityCells.includes(cell)) {
    if (event.target.value===(state.hmiProfile?.water_quality?.[cell] || '')) delete state.qualityEdits[cell];
    else state.qualityEdits[cell]=event.target.value;
  }
  if (hmiEditableCells.includes(cell)) {
    if (event.target.value===(state.hmiProfile?.fields[cell] || '')) delete state.hmiEdits[cell];
    else state.hmiEdits[cell]=event.target.value;
  }
  updateButtons(); queueDraft();
}
for (const id of qualityCells) $(id).addEventListener('input', hmiEdited);
function buildHmiEditor() {
  for (const [target,count,label] of [['night-worker-fields',2,'당직'],['day-worker-fields',8,'일근']]) {
    for (let i=1;i<=count;i++) {
      const wrapper = document.createElement('label');
      const input = document.createElement('input'); input.type='text'; input.maxLength=30; input.autocomplete='off';
      input.placeholder=`${label} ${i}`; input.setAttribute('aria-label',`${label} 근무자 ${i}`); input.disabled=true;
      input.addEventListener('input',hmiEdited); wrapper.append(input); $(target).append(wrapper);
    }
  }
  for (let i=0;i<8;i++) {
    const c='CDEFGHIJ'[i], tr=document.createElement('tr'), label=document.createElement('td'); label.textContent=`${i+1}번`; tr.append(label);
    const td=document.createElement('td'), select=document.createElement('select'); select.id=`hmi-${c}28`;
    select.setAttribute('aria-label',`${i+1}번 분리막 가동상태`); select.addEventListener('change',hmiEdited);
    for (const status of ['','가동','정지','점검']) select.add(new Option(status||'미입력',status));
    td.append(select); tr.append(td);
    for (const [row,name] of [[32,'유지세정'],[33,'알칼리세정'],[34,'산세정']]) {
      const cell=document.createElement('td'), input=document.createElement('input');
      input.type='date'; input.id=`hmi-${c}${row}`; input.setAttribute('aria-label',`${i+1}번 분리막 ${name}일자`);
      const wrapper=document.createElement('div'), display=document.createElement('span');
      wrapper.className='hmi-date-field'; display.className='hmi-date-value'; display.setAttribute('aria-hidden','true');
      input.addEventListener('input',hmiEdited);
      input.addEventListener('change',()=>refreshHmiDate(input));
      input.addEventListener('click',()=>{
        if (!input.disabled && typeof input.showPicker==='function') {
          try { input.showPicker(); } catch { /* Native calendar indicator remains available. */ }
        }
      });
      wrapper.append(input,display); cell.append(wrapper); tr.append(cell); refreshHmiDate(input);
    }
    $('hmi-carry-body').append(tr);
  }
}
async function ensureHmiProfile(date, force=false) {
  force = force || state.needHmiRefresh;
  if (!date || (state.hmiProfile?.record_date === date && !force)) return;
  const keepWorkers = state.hmiProfile?.record_date === date;
  const day = keepWorkers ? Array.from(document.querySelectorAll('#day-worker-fields input')).map(input=>input.value) : [];
  const night = keepWorkers ? Array.from(document.querySelectorAll('#night-worker-fields input')).map(input=>input.value) : [];
  const rev=++state.hmiRevision;
  state.hmiLoading=true; state.hmiProfile=null; state.hmiEdits={}; state.needHmiRefresh=false;
  document.querySelectorAll('#hmi-editor input,#hmi-editor select,#hmi-editor textarea').forEach(c=>c.disabled=true);
  $('carry-note').textContent = '전날 자료 확인 중'; $('carry-note').classList.remove('first-entry');
  $('hmi-profile-info').textContent=`${date} 작성용 전날 HMI를 불러오고 있습니다…`; updateButtons();
  try {
    const profile=await api('/api/hmi/profile',{record_date:date});
    if (rev!==state.hmiRevision) return;
    state.hmiProfile=profile; state.hmiDirty=false; seedQuality();
    document.querySelectorAll('#day-worker-fields input').forEach((input,i)=>input.value=day[i]||'');
    document.querySelectorAll('#night-worker-fields input').forEach((input,i)=>input.value=night[i]||'');
    for (const cell of hmiEditableCells) {
      const element=$(`hmi-${cell}`), value=profile.fields[cell]||'';
      if (cell.endsWith('28') && !Array.from(element.options).some(o=>o.value===value)) element.add(new Option(value,value));
      element.value=value; refreshHmiDate(element);
    }
    $('carry-note').textContent = profile.previous_name ? '전날 내용 이어 받음' : '첫 작성 · 내용 확인';
    $('carry-note').classList.toggle('first-entry', !profile.previous_name);
    $('carry-help').textContent = profile.previous_name ? '전날 내용을 이어 받았습니다. 변경된 항목만 수정하세요.' : '전날 HMI가 없습니다. 가동상태와 세정일자를 입력하세요.';
    $('hmi-profile-info').textContent=`HMI 작성일 ${date} · ${profile.previous_name ? '전날 파일: '+profile.previous_name : '전날('+profile.previous_date+') HMI 없음'}\n${profile.message}`;
  } catch(e) { if(rev===state.hmiRevision){$('hmi-profile-info').textContent=e.message;$('carry-note').textContent='자료 확인 필요';showError(e.message);} }
  finally {
    if(rev===state.hmiRevision){state.hmiLoading=false;document.querySelectorAll('#hmi-editor input,#hmi-editor select,#hmi-editor textarea').forEach(c=>c.disabled=state.busy);updateButtons();}
  }
}
$('hmi-profile-reload').addEventListener('click',async()=>{
  if(state.busy||state.hmiLoading)return;
  const date=state.inspected?.record_date;
  await ensureHmiProfile(date,true);
  queueDraft();
});
buildHmiEditor();
function renderRecovery(data) {
  state.recoveryBusy = Boolean(data.busy);
  $('recovery-list').replaceChildren();
  const items = data.items || [];
  $('recovery-message').textContent = data.busy ? '일지 생성 또는 복구 중입니다.' : items.length ? '보류된 복구가 있습니다. 원래 저장 폴더와 아래 내용을 확인하세요.' : '보류된 복구가 없습니다.';
  $('recovery-retry').hidden = !items.length;
  for (const item of items) {
    const li = document.createElement('li');
    const title = document.createElement('strong'); title.textContent = item.record_date || '기록일 확인 필요';
    const reason = document.createElement('p'); reason.textContent = item.error;
    const folders = document.createElement('p'); folders.className = 'help'; folders.textContent = (item.folders || []).join('\n');
    li.append(title, reason, folders); $('recovery-list').append(li);
  }
}
async function refreshRecovery() {
  if (state.stopped || state.recoveryLoading) return;
  state.recoveryLoading = true; updateButtons();
  try { renderRecovery(await api('/api/recovery')); }
  catch (e) {
    $('recovery-message').textContent = e.message;
    $('recovery-retry').hidden = false;
    state.recoveryBusy = false;
  }
  finally { state.recoveryLoading = false; updateButtons(); }
}
$('recovery-retry').addEventListener('click', async () => {
  if ($('recovery-retry').disabled || state.stopped) return;
  state.busy = true; updateButtons();
  $('recovery-message').textContent = '중단된 저장을 다시 확인하고 있습니다…';
  try { renderRecovery(await api('/api/recovery/retry', {})); }
  catch (e) { $('recovery-message').textContent = e.message; }
  finally { state.busy = false; updateButtons(); await refreshRecovery(); }
});
function switchTab(name, focus=false) {
  state.tab = name;
  if (name === 'settings') refreshRecovery();
  for (const tab of ['entry','settings']) {
    const active = tab === name;
    $(`${tab}-page`).hidden = !active;
    $(`${tab}-tab`).classList.toggle('active', active);
    $(`${tab}-tab`).setAttribute('aria-selected', String(active));
    $(`${tab}-tab`).tabIndex = active ? 0 : -1;
  }
  window.scrollTo({top:0,behavior:'instant'});
  if (focus) $(`${name}-tab`).focus();
}
for (const tab of ['entry','settings']) {
  $(`${tab}-tab`).addEventListener('click', () => switchTab(tab));
  $(`${tab}-tab`).addEventListener('keydown', event => {
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    switchTab(event.key === 'Home' ? 'entry' : event.key === 'End' ? 'settings' : tab === 'entry' ? 'settings' : 'entry', true);
  });
}
$('back-to-entry').addEventListener('click', () => switchTab('entry', true));
$('source-settings').addEventListener('click', () => { switchTab('settings'); $('source-details').scrollIntoView({behavior:'smooth',block:'start'}); });
$('server-stop').addEventListener('click', async () => {
  if (state.busy || state.scanning || state.hmiLoading || state.stopped) return;
  state.busy = true; updateButtons();
  try {
    await flushDrafts();
    await api('/api/shutdown', {});
    state.stopped = true;
    $('runtime-label').textContent = '서버 종료됨';
    $('file-status').textContent = '서버를 종료했습니다. 이 창을 닫아도 됩니다. 다시 사용하려면 일지작성.exe를 실행하세요.';
  } catch (e) { showError(e.message); }
  finally { state.busy = false; updateButtons(); }
});
async function initialize() {
  try {
    showSettings(await api('/api/settings'));
    if (!state.configured) {
      clearInspect();
      $('file-status').textContent=state.configurationMessage;
      switchTab('settings');
      updateButtons();
      return;
    }
    await loadFiles(true); await inspectFiles();
  }
  catch (e) { switchTab('settings'); $('file-status').textContent = e.message; showError(e.message); }
}
updateButtons();
initialize();
refreshRecovery();
window.addEventListener('pagehide',flushDraftBeacons);
document.addEventListener('visibilitychange',()=>{ if (document.hidden) flushDrafts(); });
setInterval(async () => {
  if (!state.stopped && !state.busy && state.tab === 'settings' && !document.hidden) await refreshRecovery();
  if (!state.configured || state.stopped || state.busy || state.scanning || state.savingSettings || state.tab === 'settings' || state.hmiDirty || dayWorkers().length || nightWorkers().some(Boolean) || document.hidden || allManualCells.some(id => $(id).value !== '')) return;
  try { if (await loadFiles(state.followingLatest, true)) await inspectFiles(); }
  catch (e) { ++state.fileRevision; ++state.revision; clearInspect(); $('file-status').textContent = e.message; }
}, 30000);

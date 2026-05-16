/* SPDX-License-Identifier: Apache-2.0 */
/* OpenFocus Terminal Panel (browser UI)
 * ttyd + tmux only.
 * Exposes: window.OpenFocusRemoteTerminal.mount(el, { spaceId })
 */

(function(){
  function $(sel, root){ return (root||document).querySelector(sel); }

  function esc(s){
    const x = String(s ?? '');
    return x.replace(/[&<>"']/g, (c)=> ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c] || c));
  }

  function bytesToB64(u8){
    let s = '';
    for(let i=0;i<u8.length;i++) s += String.fromCharCode(u8[i]);
    return btoa(s);
  }

  async function fetchJson(url, opts){
    const r = await fetch(url, opts);
    if(!r.ok){
      const t = await r.text().catch(()=> '');
      throw new Error(t || ('HTTP ' + r.status));
    }
    return await r.json();
  }

  function shortId(id){
    const s = String(id||'');
    if(s.length <= 8) return s || 'terminal';
    return s.slice(0, 4) + '…' + s.slice(-2);
  }

  function normalizeName(s){
    return String(s||'').trim();
  }

  // Inspired by the PUA Proactivity Engine skill:
  // https://mcpmarket.com/tools/skills/pua-proactivity-engine
  const PUA_PROACTIVITY_PROMPT = 'You are a P8-level senior engineer and the final owner of this task: be proactive, drive the work end-to-end, do not stop at superficial fixes or unverified guesses, do not ask the user to intervene until you have exhausted available investigation paths, inspect source code and dependencies, consult official documentation when needed, identify and verify the root cause, escalate your reasoning when repeated attempts fail, try alternative paths, and validate every fix with appropriate tests, builds, runtime checks, or API/curl verification before reporting completion.';

  function mount(rootEl, opts){
    const spaceId = Number(opts && opts.spaceId ? opts.spaceId : 0);
    const taskPublicId = String(opts && opts.taskPublicId ? opts.taskPublicId : '').trim();
    const apiBase = String(opts && opts.apiBase ? opts.apiBase : `/api/agent_spaces/${spaceId}/terminals`).replace(/\/+$/, '');
    const mode = String(opts && opts.mode ? opts.mode : 'agent_space');
    const isInspiration = mode === 'inspiration';
    const commandApi = String(opts && opts.commandApi ? opts.commandApi : `/api/agent_spaces/${spaceId}/start_agent_command`).replace(/\/+$/, '');
    const promptApi = String(opts && opts.promptApi ? opts.promptApi : '/api/agent_space_prompts').replace(/\/+$/, '');
    let startAgentCommand = String(opts && opts.startAgentCommand ? opts.startAgentCommand : '').trim();
    let customPrompts = [];
    const goalResources = Array.isArray(opts && opts.goalResources)
      ? opts.goalResources.map((r)=> ({ id: Number(r && r.id ? r.id : 0), title: String(r && r.title ? r.title : '') })).filter((r)=> r.id && r.title)
      : [];
    const goalSelectOptionsHtml = goalResources.length
      ? '<option value="">Choose a resource…</option>' + goalResources.map((r)=> `<option value="${esc(r.title)}">${esc(r.title)}</option>`).join('')
      : '<option value="">No resources available</option>';
    if(!rootEl) throw new Error('mount element required');
    if(!spaceId) throw new Error('spaceId required');

    // AGENT 模式按 terminal 生效（不是全局/space 全局）。
    function agentModeKey(terminalId){
      const tid = String(terminalId || '').trim();
      return `openfocus.${mode}.terminal.agent_mode.${String(spaceId)}.${tid}`;
    }

    function mouseModeKey(terminalId){
      const tid = String(terminalId || '').trim();
      return `openfocus.${mode}.terminal.mouse_mode.${String(spaceId)}.${tid}`;
    }

    function loadAgentMode(terminalId){
      try{ return (localStorage.getItem(agentModeKey(terminalId)) || '') === '1'; }catch(_){ return false; }
    }

    function saveAgentMode(terminalId, v){
      try{ localStorage.setItem(agentModeKey(terminalId), v ? '1' : '0'); }catch(_){ }
    }

    function loadMouseMode(terminalId){
      try{
        const v = localStorage.getItem(mouseModeKey(terminalId));
        return v === null ? true : v === '1';
      }catch(_){ return true; }
    }

    function saveMouseMode(terminalId, v){
      try{ localStorage.setItem(mouseModeKey(terminalId), v ? '1' : '0'); }catch(_){ }
    }

    function buildAgentPrefix(){
      // 必须是单行：不能包含 \n/\r，否则会提前提交或破坏 TUI。
      const base = (location && location.origin) ? String(location.origin) : '';
      if(opts && opts.agentPrefix) return String(opts.agentPrefix || '');
      const tid = taskPublicId || '';
      const parts = [];
      if(tid) parts.push(`taskId=${tid}`);
      if(base) parts.push(`openfocus=${base}`);
      parts.push('进度上报: POST /api/agent/events; 最终结果: POST /api/skills/focus_report');
      return parts.join(' · ');
    }

    function buildPasteText(kind){
      const prefix = buildAgentPrefix();
      const k = String(kind || 'context');
      if(k === 'draft_summary') return `[OpenFocus Summary Request] ${String(opts && opts.draftSummaryPrompt ? opts.draftSummaryPrompt : '')}`;
      if(k === 'lessons') return `[OpenFocus Lessons]\n${prefix}`;
      if(k === 'pua') return PUA_PROACTIVITY_PROMPT;
      return prefix;
    }

    rootEl.innerHTML = `
      <div class="rt-shell">
        <div class="rt-wrap">
          <div class="rt-top">
            <div class="rt-tabs" id="rt-tabs"></div>
            <div class="rt-actions">
              <div class="rt-status" id="rt-status">—</div>
              <button type="button" class="btn-ghost" id="rt-new" title="New terminal">+</button>
            </div>
          </div>
          <div class="rt-body" id="rt-body"></div>
        </div>
        <div class="rt-side">
          <div class="rt-side-title">prompt zone</div>
          ${isInspiration ? '' : '<label class="rt-agent-switch" title="enable agent mode"><input type="checkbox" id="rt-agent-switch" /><span class="rt-agent-slider" aria-hidden="true"></span><span class="rt-agent-text">agent mode</span></label>'}
          <label class="rt-agent-switch rt-mouse-switch" title="scroll: wheel scrolls tmux history. copy: browser drag-copy friendly."><input type="checkbox" id="rt-mouse-switch" /><span class="rt-agent-slider" aria-hidden="true"></span><span class="rt-agent-text" id="rt-mouse-text">scroll</span></label>
          ${isInspiration ? '<button type="button" class="btn-ghost" id="rt-draft-summary" title="send the summary instructions as plain text into this terminal without pressing enter.">summary</button><button type="button" class="btn-primary insp-create-btn" id="rt-create-goal" style="margin-top:auto;" title="choose a resource and generate a reviewable goal/tasks draft from it.">create goal</button>' : '<div class="rt-zone-divider" aria-hidden="true"></div><div class="rt-zone-section"><button type="button" class="btn-ghost" id="rt-lessons">draw lessons</button><button type="button" class="btn-ghost" id="rt-pua" title="inject a proactivity escalation prompt into the active terminal.">pua</button></div><div class="rt-zone-divider" aria-hidden="true"></div><div class="rt-zone-section"><div class="rt-prompt-list" id="rt-custom-prompts"><div class="rt-prompt-empty">loading prompts...</div></div></div><div class="rt-start-agent-row"><button type="button" class="btn-primary rt-start-agent-btn" id="rt-start-agent" title="run the configured agent command in a new terminal and turn on agent mode.">start agent</button><button type="button" class="btn-ghost rt-start-agent-edit" id="rt-start-agent-edit" title="edit start agent command" aria-label="edit start agent command">✏</button></div>'}
        </div>
        ${isInspiration ? '<div class="rt-modal-backdrop" id="rt-create-goal-modal" hidden><div class="rt-modal-card"><div class="rt-modal-head"><strong>Create Goal</strong><button type="button" class="btn-ghost" id="rt-create-goal-modal-x">×</button></div><div class="rt-modal-body"><label for="rt-create-goal-select">Resource</label><select id="rt-create-goal-select">' + goalSelectOptionsHtml + '</select><div class="rt-goal-hint">Choose one resource file to generate a reviewable draft for Publish.</div></div><div class="rt-modal-actions"><button type="button" class="btn-ghost" id="rt-create-goal-cancel">Cancel</button><button type="button" class="btn-primary insp-create-btn" id="rt-create-goal-confirm">Create Goal</button></div></div></div>' : ''}
      </div>
    `;

    const tabsEl = $('#rt-tabs', rootEl);
    const bodyEl = $('#rt-body', rootEl);
    const statusEl = $('#rt-status', rootEl);
    const btnNew = $('#rt-new', rootEl);
    const agentSwitch = $('#rt-agent-switch', rootEl);
    const mouseSwitch = $('#rt-mouse-switch', rootEl);
    const mouseText = $('#rt-mouse-text', rootEl);
    const btnLessons = $('#rt-lessons', rootEl);
    const btnPua = $('#rt-pua', rootEl);
    const customPromptsEl = $('#rt-custom-prompts', rootEl);
    const btnStartAgent = $('#rt-start-agent', rootEl);
    const btnStartAgentEdit = $('#rt-start-agent-edit', rootEl);
    const btnDraftSummary = $('#rt-draft-summary', rootEl);
    const btnCreateGoal = $('#rt-create-goal', rootEl);
    const createGoalModal = $('#rt-create-goal-modal', rootEl);
    const createGoalModalX = $('#rt-create-goal-modal-x', rootEl);
    const createGoalCancel = $('#rt-create-goal-cancel', rootEl);
    const createGoalConfirm = $('#rt-create-goal-confirm', rootEl);
    const createGoalSelect = $('#rt-create-goal-select', rootEl);

    const terminals = new Map(); // terminal_id -> { terminalId, name, tabEl, nameEl, viewEl, iframeEl }
    let activeId = '';
    let initialLoadPromise = null;

    function setStatus(s){ if(statusEl) statusEl.textContent = String(s||'—'); }

    function activeTerminal(){ return terminals.get(activeId) || null; }

    function startAgentCommandLabel(){
      const cmd = String(startAgentCommand || '').trim();
      return cmd ? `Start Agent: ${cmd}` : 'Set Start Agent command';
    }

    function applyStartAgentUi(){
      if(btnStartAgent) btnStartAgent.title = startAgentCommandLabel();
      if(btnStartAgentEdit) btnStartAgentEdit.title = startAgentCommandLabel();
    }

    function renderCustomPrompts(){
      if(isInspiration || !customPromptsEl) return;
      if(!customPrompts.length){
        customPromptsEl.innerHTML = '<div class="rt-prompt-empty">no prompts</div>';
        return;
      }
      customPromptsEl.innerHTML = customPrompts.map((p)=> {
        const id = Number(p && p.id ? p.id : 0);
        const title = esc(String(p && p.title ? p.title : 'Prompt'));
        const content = esc(String(p && p.content ? p.content : ''));
        return `<button type="button" class="btn-ghost rt-prompt-btn" data-prompt-id="${id}" title="${title}: ${content}">${title}</button>`;
      }).join('');
    }

    async function loadCustomPrompts(){
      if(isInspiration || !customPromptsEl) return;
      try{
        const data = await fetchJson(promptApi);
        customPrompts = Array.isArray(data && data.items) ? data.items : [];
      }catch(_){
        customPrompts = [];
      }
      renderCustomPrompts();
    }

    function customPromptText(prompt){
      const content = String(prompt && prompt.content ? prompt.content : '').trim();
      return content ? content.replace(/\s+/g, ' ').trim() : '';
    }

    function applyAgentUi(){
      const it = activeTerminal();
      const on = !!(it && it.__agent_mode);
      if(agentSwitch && agentSwitch instanceof HTMLInputElement){
        agentSwitch.checked = on;
      }
    }

    function applyMouseUi(){
      const it = activeTerminal();
      const on = it ? it.__mouse_mode !== false : true;
      if(mouseSwitch && mouseSwitch instanceof HTMLInputElement){
        mouseSwitch.checked = on;
        mouseSwitch.disabled = !it;
      }
      if(mouseText){
        mouseText.textContent = on ? 'scroll' : 'copy';
      }
    }

    async function injectInputBytes(it, u8){
      if(!it) return;
      const data_b64 = bytesToB64(u8);
      await fetchJson(`${apiBase}/${encodeURIComponent(it.terminalId)}/inject`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data_b64 }),
      });
    }

    async function injectPromptToTerminal(target, text, options){
      const it = target || activeTerminal();
      if(!it) return false;
      const opts2 = options || {};
      const s = String(text || '');
      if(!s) return false;
      const submit = !!opts2.submit;
      const bracketedPaste = opts2.bracketedPaste !== false;
      const payload = (bracketedPaste ? `\x1b[200~${s}\x1b[201~` : s) + (submit ? '\r' : '');
      const enc = new TextEncoder();
      await injectInputBytes(it, enc.encode(payload));
      if(opts2.focus !== false) focusActive();
      return true;
    }

    async function saveStartAgentCommand(command){
      const next = String(command || '').trim();
      if(next.length > 2000) throw new Error('command is too long (<=2000)');
      const data = await fetchJson(commandApi, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start_agent_command: next }),
      });
      startAgentCommand = String(data && typeof data.start_agent_command !== 'undefined' ? data.start_agent_command : next).trim();
      applyStartAgentUi();
      try{ localStorage.setItem('openfocus:last_start_agent_command', startAgentCommand); }catch(_){ }
      return startAgentCommand;
    }

    async function editStartAgentCommand(){
      const cur = String(startAgentCommand || '').trim();
      const next = prompt('Start Agent command', cur || 'coco -y');
      if(next === null) return '';
      const saved = await saveStartAgentCommand(next);
      toast(saved ? 'Start Agent command saved' : 'Start Agent command cleared');
      return saved;
    }

    function pasteToActive(text){
      const s = String(text || '');
      if(!s) return;
      void injectPromptToTerminal(activeTerminal(), s, { bracketedPaste: true, focus: true }).catch((err)=>{
        try{ console.warn('OpenFocus prompt injection failed:', err); }catch(_){ }
        try{
          void navigator.clipboard.writeText(s)
            .then(()=> toast('Auto-injection failed. Prompt copied to clipboard; paste it into the terminal manually.'))
            .catch(()=> toast('Auto-injection failed, and copying to clipboard also failed. Please refresh and try again.'));
        }catch(_){
          toast('Auto-injection failed, and copying to clipboard also failed. Please refresh and try again.');
        }
      });
    }

    function openCreateGoalModal(){
      if(!createGoalModal) return;
      createGoalModal.hidden = false;
      try{ createGoalSelect && createGoalSelect.focus && createGoalSelect.focus(); }catch(_){ }
    }

    function closeCreateGoalModal(){
      if(createGoalModal) createGoalModal.hidden = true;
      if(createGoalSelect && 'value' in createGoalSelect) createGoalSelect.value = '';
      focusActive();
    }

    function selectedGoalResourceText(){
      return String(createGoalSelect && 'value' in createGoalSelect ? createGoalSelect.value : '').trim();
    }

    function submitCreateGoalModal(){
      if(!(opts && typeof opts.createGoalFromResource === 'function')) return;
      if(!goalResources.length){ toast('No resources to use'); return; }
      const value = selectedGoalResourceText();
      if(!value){
        toast('Choose a resource first');
        try{ createGoalSelect && createGoalSelect.focus && createGoalSelect.focus(); }catch(_){ }
        return;
      }
      closeCreateGoalModal();
      void Promise.resolve(opts.createGoalFromResource(value)).catch((err)=> toast(String(err && err.message ? err.message : err || 'create failed')));
    }

    function syncTtydAgentMode(it){
      if(isInspiration) return;
      if(!it || !it.iframeEl) return;
      const prefix = buildAgentPrefix();
      try{
        fetchJson(`${apiBase}/${encodeURIComponent(it.terminalId)}/agent_mode`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: !!it.__agent_mode, prefix }),
        }).catch(()=>{});
      }catch(_){ }
      try{
        it.iframeEl.contentWindow && it.iframeEl.contentWindow.postMessage({
          type: 'openfocus:ttyd-agent-mode',
          enabled: !!it.__agent_mode,
          prefix,
          injectUrl: `${apiBase}/${encodeURIComponent(it.terminalId)}/inject`,
        }, window.location.origin);
      }catch(_){ }
    }

    async function syncMouseMode(it){
      if(!it) return false;
      const enabled = it.__mouse_mode !== false;
      const data = await fetchJson(`${apiBase}/${encodeURIComponent(it.terminalId)}/mouse_mode`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      it.__mouse_mode = !!(data && typeof data.enabled !== 'undefined' ? data.enabled : enabled);
      saveMouseMode(it.terminalId, it.__mouse_mode);
      applyMouseUi();
      return it.__mouse_mode;
    }

    function attachTtydAgentModeHook(it){
      if(isInspiration) return;
      if(!it || !it.iframeEl) return;
      try{ it.iframeEl.addEventListener('load', ()=> syncTtydAgentMode(it)); }catch(_){ }
      setTimeout(()=> syncTtydAgentMode(it), 300);
      setTimeout(()=> syncTtydAgentMode(it), 1200);
    }

    function focusActive(){
      const it = activeTerminal();
      if(!it) return;
      try{ it.iframeEl && it.iframeEl.contentWindow && it.iframeEl.contentWindow.focus(); }catch(_){ }
    }

    function activate(terminalId){
      const tid = String(terminalId||'');
      if(!tid) return;
      activeId = tid;
      for(const [id, it] of terminals.entries()){
        const on = id === tid;
        if(it.tabEl) it.tabEl.classList.toggle('active', on);
        if(it.viewEl) it.viewEl.classList.toggle('rt-hidden', !on);
      }
      const it = terminals.get(tid);
      if(it){
        focusActive();
        applyAgentUi();
        applyMouseUi();
        syncTtydAgentMode(it);
      }
    }

    function enableAgentModeForTerminal(it){
      if(isInspiration || !it) return;
      it.__agent_mode = true;
      saveAgentMode(it.terminalId, true);
      applyAgentUi();
      syncTtydAgentMode(it);
    }

    async function startAgent(){
      if(isInspiration) return;
      let cmd = String(startAgentCommand || '').trim();
      if(!cmd){
        cmd = await editStartAgentCommand();
        if(!cmd) return;
      }
      const it = await createNew();
      if(!it){ toast('terminal unavailable'); return; }
      enableAgentModeForTerminal(it);
      await injectPromptToTerminal(it, cmd, { bracketedPaste: false, submit: true, focus: true });
      toast('Agent started');
    }

    function isNameTaken(name, exceptTid){
      const n = normalizeName(name);
      if(!n) return false;
      for(const [id, it] of terminals.entries()){
        if(exceptTid && String(exceptTid) === String(id)) continue;
        if(normalizeName(it.name) === n) return true;
      }
      return false;
    }

    async function renameTerminal(tid, newName){
      const name = normalizeName(newName);
      if(!name){ alert('名字不能为空'); return null; }
      if(isNameTaken(name, tid)){
        alert('名字已存在（同一空间内不可重复）');
        return null;
      }
      const data = await fetchJson(`${apiBase}/${encodeURIComponent(tid)}/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      return (data && data.terminal) ? data.terminal : null;
    }

    function addTab(terminalId, name, opts){
      const tid = String(terminalId||'');
      if(!tid || terminals.has(tid)) return null;

      const cfg = opts || {};
      const embedUrl = String(cfg.embed_url || cfg.embedUrl || '').trim();
      if(!embedUrl){
        setStatus('terminal unavailable');
        return null;
      }

      const tab = document.createElement('div');
      tab.className = 'rt-tab';
      const nm = normalizeName(name) || shortId(tid);
      tab.innerHTML = `<span class="rt-name" title="Double click to rename">${esc(nm)}</span><span class="rt-x" title="Close">×</span>`;
      tabsEl.appendChild(tab);
      const nameEl = tab.querySelector('.rt-name');

      const view = document.createElement('div');
      view.className = 'rt-term rt-hidden';
      bodyEl.appendChild(view);

      const iframeEl = document.createElement('iframe');
      iframeEl.className = 'rt-ttyd-frame';
      iframeEl.setAttribute('title', nm);
      iframeEl.setAttribute('allow', 'clipboard-read; clipboard-write');
      iframeEl.src = embedUrl;
      view.appendChild(iframeEl);

      const it = { terminalId: tid, name: nm, backend: 'ttyd', embedUrl, iframeEl, tabEl: tab, nameEl, viewEl: view };
      it.__agent_mode = isInspiration ? false : loadAgentMode(tid);
      it.__mouse_mode = loadMouseMode(tid);
      terminals.set(tid, it);
      attachTtydAgentModeHook(it);
      setTimeout(()=> syncMouseMode(it).catch(()=>{}), 50);

      tab.addEventListener('click', (e)=>{
        const isClose = (e && e.target && (e.target.classList && e.target.classList.contains('rt-x')));
        if(isClose) return;
        activate(tid);
      });

      nameEl?.addEventListener('dblclick', async (e)=>{
        if(e) e.stopPropagation();
        const cur = normalizeName(it.name) || '';
        const next = prompt('重命名 Terminal（同一空间内不可重复）', cur);
        if(next === null) return;
        try{
          const res = await renameTerminal(tid, next);
          if(!res) return;
          it.name = normalizeName(res.name) || it.name;
          if(it.nameEl) it.nameEl.textContent = it.name;
        }catch(err){
          alert('重命名失败：' + String(err && err.message ? err.message : err));
        }
      });
      tab.querySelector('.rt-x')?.addEventListener('click', async (e)=>{
        if(e) e.stopPropagation();
        await closeTerminal(tid);
      });

      return it;
    }

    async function closeTerminal(terminalId){
      const tid = String(terminalId||'');
      const it = terminals.get(tid);
      if(!it) return;
      try{
        await fetchJson(`${apiBase}/${encodeURIComponent(tid)}/close`, { method: 'POST' });
      }catch(e){
        try{ toast('关闭失败'); }catch(_){ }
        alert('关闭失败：' + String(e && e.message ? e.message : e));
        return;
      }

      try{ it.viewEl.remove(); }catch(_){ }
      try{ it.tabEl.remove(); }catch(_){ }
      terminals.delete(tid);

      // pick another tab
      if(activeId === tid){
        const next = terminals.keys().next();
        activeId = '';
        if(!next.done) activate(next.value);
      }
      if(terminals.size === 0){
        setStatus('no terminals');
      }
    }

    async function createNew(){
      setStatus('starting…');
      try{
        const data = await fetchJson(`${apiBase}/new`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        const tid = String(data && data.terminal && data.terminal.terminal_id ? data.terminal.terminal_id : '');
        const name = String(data && data.terminal && data.terminal.name ? data.terminal.name : '');
        if(!tid) throw new Error('terminal_id missing');
        const it = addTab(tid, name, data && data.terminal ? data.terminal : {});
        if(!it) throw new Error('ttyd embed_url missing');
        activate(tid);
        setStatus('ready');
        return it;
      }catch(e){
        setStatus('start failed');
        alert('创建终端失败：' + String(e && e.message ? e.message : e));
        return null;
      }
    }

    async function loadExisting(){
      setStatus('loading…');
      let data;
      try{
        data = await fetchJson(apiBase);
      }catch(e){
        setStatus('load failed');
        return;
      }
      const online = !!(data && data.companion && data.companion.online);
      setStatus(online ? 'Companion online' : 'Companion offline');
      const arr = Array.isArray(data && data.terminals) ? data.terminals : [];
      for(const t of arr){
        const tid = String(t.terminal_id||'');
        const name = String(t.name||'');
        if(!tid) continue;
        addTab(tid, name, { embed_url: t.embed_url });
      }
      if(terminals.size){
        const first = terminals.keys().next();
        if(!first.done) activate(first.value);
      }else if(online){
        await createNew();
      }
    }

    async function ensureTerminalReady(){
      try{
        if(initialLoadPromise) await initialLoadPromise;
      }catch(_){ }
      let it = activeTerminal();
      if(it) return it;
      await createNew();
      it = activeTerminal();
      return it || null;
    }

    btnNew?.addEventListener('click', createNew);

    agentSwitch?.addEventListener('change', ()=>{
      const it = activeTerminal();
      if(!it){
        if(agentSwitch && agentSwitch instanceof HTMLInputElement) agentSwitch.checked = false;
        return;
      }
      it.__agent_mode = !!(agentSwitch && agentSwitch instanceof HTMLInputElement && agentSwitch.checked);
      saveAgentMode(it.terminalId, it.__agent_mode);
      applyAgentUi();
      syncTtydAgentMode(it);
      try{ toast(it.__agent_mode ? 'Agent Mode: ON' : 'Agent Mode: OFF'); }catch(_){ }
      focusActive();
    });
    mouseSwitch?.addEventListener('change', ()=>{
      const it = activeTerminal();
      if(!it){ applyMouseUi(); return; }
      it.__mouse_mode = !!(mouseSwitch && mouseSwitch instanceof HTMLInputElement && mouseSwitch.checked);
      saveMouseMode(it.terminalId, it.__mouse_mode);
      applyMouseUi();
      void syncMouseMode(it)
        .then((on)=> toast(on ? 'scroll: on' : 'copy: on'))
        .catch((err)=>{
          it.__mouse_mode = !it.__mouse_mode;
          saveMouseMode(it.terminalId, it.__mouse_mode);
          applyMouseUi();
          toast(String(err && err.message ? err.message : err || 'mouse mode failed'));
        })
        .finally(()=> focusActive());
    });
    btnLessons?.addEventListener('click', ()=> pasteToActive(buildPasteText('lessons')));
    btnPua?.addEventListener('click', ()=> pasteToActive(buildPasteText('pua')));
    customPromptsEl?.addEventListener('click', (e)=> {
      const target = e && e.target && e.target.closest ? e.target.closest('[data-prompt-id]') : null;
      if(!target) return;
      const id = Number(target.getAttribute('data-prompt-id') || 0);
      const prompt = customPrompts.find((p)=> Number(p && p.id ? p.id : 0) === id);
      pasteToActive(customPromptText(prompt));
    });
    btnStartAgent?.addEventListener('click', ()=> {
      void startAgent().catch((err)=> toast(String(err && err.message ? err.message : err || 'start failed')));
    });
    btnStartAgentEdit?.addEventListener('click', ()=> {
      void editStartAgentCommand().catch((err)=> toast(String(err && err.message ? err.message : err || 'save failed')));
    });
    btnDraftSummary?.addEventListener('click', ()=> {
      void injectPromptToTerminal(activeTerminal(), buildPasteText('draft_summary'), { bracketedPaste: false, submit: false, focus: true })
        .then((ok)=>{ if(ok) toast('Summary prompt sent'); })
        .catch((err)=> toast(String(err && err.message ? err.message : err || 'inject failed')));
    });
    btnCreateGoal?.addEventListener('click', openCreateGoalModal);
    createGoalModalX?.addEventListener('click', closeCreateGoalModal);
    createGoalCancel?.addEventListener('click', closeCreateGoalModal);
    createGoalConfirm?.addEventListener('click', submitCreateGoalModal);
    createGoalModal?.addEventListener('click', (e)=>{ if(e.target === createGoalModal) closeCreateGoalModal(); });
    createGoalSelect?.addEventListener('keydown', (e)=>{
      if(e && e.key === 'Enter'){
        e.preventDefault();
        submitCreateGoalModal();
      }
    });

    window.addEventListener('resize', focusActive);
    window.addEventListener('openfocus:agent-space-layout-changed', focusActive);
    window.addEventListener('pageshow', focusActive);
    document.addEventListener('visibilitychange', ()=>{
      if(document.visibilityState === 'visible') focusActive();
    });

    const api = {
      createNew,
      closeTerminal,
      activate,
      injectPromptToTerminal: async (text, options)=> {
        const it = await ensureTerminalReady();
        return injectPromptToTerminal(it, text, options);
      },
    };
    try{ rootEl.__openfocusRemoteTerminal = api; }catch(_){ }

    initialLoadPromise = loadExisting();
    applyAgentUi();
    applyMouseUi();
    applyStartAgentUi();
    void loadCustomPrompts();
    return api;
  }

  window.OpenFocusRemoteTerminal = { mount };
})();

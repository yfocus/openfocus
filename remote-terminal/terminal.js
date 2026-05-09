/* OpenFocus Remote Terminal (browser UI)
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

  function mount(rootEl, opts){
    const spaceId = Number(opts && opts.spaceId ? opts.spaceId : 0);
    const taskPublicId = String(opts && opts.taskPublicId ? opts.taskPublicId : '').trim();
    if(!rootEl) throw new Error('mount element required');
    if(!spaceId) throw new Error('spaceId required');

    // AGENT 模式按 terminal 生效（不是全局/space 全局）。
    function agentModeKey(terminalId){
      const tid = String(terminalId || '').trim();
      return `openfocus.agent_space.terminal.agent_mode.${String(spaceId)}.${tid}`;
    }

    function loadAgentMode(terminalId){
      try{ return (localStorage.getItem(agentModeKey(terminalId)) || '') === '1'; }catch(_){ return false; }
    }

    function saveAgentMode(terminalId, v){
      try{ localStorage.setItem(agentModeKey(terminalId), v ? '1' : '0'); }catch(_){ }
    }

    function buildAgentPrefix(){
      // 必须是单行：不能包含 \n/\r，否则会提前提交或破坏 TUI。
      const base = (location && location.origin) ? String(location.origin) : '';
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
      if(k === 'lessons') return `[OpenFocus Lessons]\n${prefix}`;
      if(k === 'custom') return `[OpenFocus Context]\n${prefix}`;
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
          <div class="rt-side-title">Prompt Zone</div>
          <label class="rt-agent-switch" title="Enable Agent Mode">
            <input type="checkbox" id="rt-agent-switch" />
            <span class="rt-agent-slider" aria-hidden="true"></span>
            <span class="rt-agent-text">Agent Mode</span>
          </label>
          <button type="button" class="btn-ghost" id="rt-lessons">Draw Lessons</button>
          <button type="button" class="btn-ghost" id="rt-custom">Custom</button>
        </div>
      </div>
    `;

    const tabsEl = $('#rt-tabs', rootEl);
    const bodyEl = $('#rt-body', rootEl);
    const statusEl = $('#rt-status', rootEl);
    const btnNew = $('#rt-new', rootEl);
    const agentSwitch = $('#rt-agent-switch', rootEl);
    const btnLessons = $('#rt-lessons', rootEl);
    const btnCustom = $('#rt-custom', rootEl);

    const terminals = new Map(); // terminal_id -> { terminalId, name, tabEl, nameEl, viewEl, iframeEl }
    let activeId = '';

    function setStatus(s){ if(statusEl) statusEl.textContent = String(s||'—'); }

    function activeTerminal(){ return terminals.get(activeId) || null; }

    function applyAgentUi(){
      const it = activeTerminal();
      const on = !!(it && it.__agent_mode);
      if(agentSwitch && agentSwitch instanceof HTMLInputElement){
        agentSwitch.checked = on;
      }
    }

    async function injectInputBytes(it, u8){
      if(!it) return;
      const data_b64 = bytesToB64(u8);
      await fetchJson(`/api/agent_spaces/${spaceId}/terminals/${encodeURIComponent(it.terminalId)}/inject`, {
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

    function pasteToActive(text){
      const s = String(text || '');
      if(!s) return;
      void injectPromptToTerminal(activeTerminal(), s, { bracketedPaste: true, focus: true }).catch(()=>{
        try{ navigator.clipboard.writeText(s); toast('注入失败，已复制'); }catch(_){ }
      });
    }

    function syncTtydAgentMode(it){
      if(!it || !it.iframeEl) return;
      const prefix = buildAgentPrefix();
      try{
        fetchJson(`/api/agent_spaces/${spaceId}/terminals/${encodeURIComponent(it.terminalId)}/agent_mode`, {
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
          injectUrl: `/api/agent_spaces/${spaceId}/terminals/${encodeURIComponent(it.terminalId)}/inject`,
        }, window.location.origin);
      }catch(_){ }
    }

    function attachTtydAgentModeHook(it){
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
        syncTtydAgentMode(it);
      }
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
        alert('名字已存在（同一 AgentSpace 内不可重复）');
        return null;
      }
      const data = await fetchJson(`/api/agent_spaces/${spaceId}/terminals/${encodeURIComponent(tid)}/rename`, {
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
      it.__agent_mode = loadAgentMode(tid);
      terminals.set(tid, it);
      attachTtydAgentModeHook(it);

      tab.addEventListener('click', (e)=>{
        const isClose = (e && e.target && (e.target.classList && e.target.classList.contains('rt-x')));
        if(isClose) return;
        activate(tid);
      });

      nameEl?.addEventListener('dblclick', async (e)=>{
        if(e) e.stopPropagation();
        const cur = normalizeName(it.name) || '';
        const next = prompt('重命名 Terminal（同一 AgentSpace 内不可重复）', cur);
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
        await fetchJson(`/api/agent_spaces/${spaceId}/terminals/${encodeURIComponent(tid)}/close`, { method: 'POST' });
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
        const data = await fetchJson(`/api/agent_spaces/${spaceId}/terminals/new`, { method: 'POST' });
        const tid = String(data && data.terminal && data.terminal.terminal_id ? data.terminal.terminal_id : '');
        const name = String(data && data.terminal && data.terminal.name ? data.terminal.name : '');
        if(!tid) throw new Error('terminal_id missing');
        const it = addTab(tid, name, data && data.terminal ? data.terminal : {});
        if(!it) throw new Error('ttyd embed_url missing');
        activate(tid);
        setStatus('ready');
      }catch(e){
        setStatus('start failed');
        alert('创建终端失败：' + String(e && e.message ? e.message : e));
      }
    }

    async function loadExisting(){
      setStatus('loading…');
      let data;
      try{
        data = await fetchJson(`/api/agent_spaces/${spaceId}/terminals`);
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
    btnLessons?.addEventListener('click', ()=> pasteToActive(buildPasteText('lessons')));
    btnCustom?.addEventListener('click', ()=> pasteToActive(buildPasteText('custom')));

    window.addEventListener('resize', focusActive);
    window.addEventListener('openfocus:agent-space-layout-changed', focusActive);
    window.addEventListener('pageshow', focusActive);
    document.addEventListener('visibilitychange', ()=>{
      if(document.visibilityState === 'visible') focusActive();
    });

    loadExisting();
    applyAgentUi();

    const api = {
      createNew,
      closeTerminal,
      activate,
      injectPromptToTerminal: (text, options)=> injectPromptToTerminal(activeTerminal(), text, options),
    };
    try{ rootEl.__openfocusRemoteTerminal = api; }catch(_){ }
    return api;
  }

  window.OpenFocusRemoteTerminal = { mount };
})();

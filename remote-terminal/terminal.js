/* OpenFocus Remote Terminal (browser UI)
 * - Depends on global: Terminal, FitAddon
 * - Exposes: window.OpenFocusRemoteTerminal.mount(el, { spaceId })
 */

(function(){
  function $(sel, root){ return (root||document).querySelector(sel); }

  function esc(s){
    const x = String(s ?? '');
    return x.replace(/[&<>"']/g, (c)=> ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c] || c));
  }

  function wsUrl(path){
    const proto = (location.protocol === 'https:') ? 'wss:' : 'ws:';
    return proto + '//' + location.host + path;
  }

  function bytesToB64(u8){
    let s = '';
    for(let i=0;i<u8.length;i++) s += String.fromCharCode(u8[i]);
    return btoa(s);
  }

  function b64ToBytes(b64){
    const bin = atob(String(b64||''));
    const out = new Uint8Array(bin.length);
    for(let i=0;i<bin.length;i++) out[i] = bin.charCodeAt(i);
    return out;
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

  function createTerminal(){
    if(typeof Terminal === 'undefined'){
      throw new Error('xterm 未加载：全局变量 Terminal 不存在');
    }
    if(typeof FitAddon === 'undefined' || !FitAddon || !FitAddon.FitAddon){
      throw new Error('xterm-addon-fit 未加载：全局变量 FitAddon 不存在');
    }
    const term = new Terminal({
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
      fontSize: 12,
      cursorBlink: true,
      convertEol: true,
      theme: {
        background: 'rgba(0,0,0,0)',
        foreground: '#d7ffe9',
        cursor: '#00e5ff',
        selectionBackground: 'rgba(0,229,255,0.22)',
      },
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    return { term, fit };
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
          <button type="button" class="btn-ghost" id="rt-lessons" disabled>Draw Lessons</button>
          <button type="button" class="btn-ghost" id="rt-custom" disabled>Custom</button>
        </div>
      </div>
    `;

    const tabsEl = $('#rt-tabs', rootEl);
    const bodyEl = $('#rt-body', rootEl);
    const statusEl = $('#rt-status', rootEl);
    const btnNew = $('#rt-new', rootEl);
    const agentSwitch = $('#rt-agent-switch', rootEl);

    const terminals = new Map(); // terminal_id -> { terminalId, name, tabEl, nameEl, viewEl, term, fit, ws, decoder }
    let activeId = '';

    function setStatus(s){ if(statusEl) statusEl.textContent = String(s||'—'); }

    function applyAgentUi(){
      const it = terminals.get(activeId);
      const on = !!(it && it.__agent_mode);
      if(agentSwitch && agentSwitch instanceof HTMLInputElement){
        agentSwitch.checked = on;
      }
    }

    function isCtrlC(data){
      return String(data||'').indexOf('\x03') >= 0;
    }

    function sendInputBytes(it, u8){
      if(!it || !it.ws || it.ws.readyState !== 1) return;
      const data_b64 = bytesToB64(u8);
      try{ it.ws.send(JSON.stringify({ type: 'input', data_b64 })); }catch(_){ }
    }

    function focusActive(){
      const it = terminals.get(activeId);
      if(!it) return;
      try{ it.term.focus(); }catch(_){ }
    }

    function _doFit(it){
      if(!it || !it.fit) return { cols: 0, rows: 0 };
      try{ it.fit.fit(); }catch(_){ }
      const dims = it.fit.proposeDimensions ? it.fit.proposeDimensions() : null;
      const cols = (dims && dims.cols) ? dims.cols : (it.term && it.term.cols ? it.term.cols : 0);
      const rows = (dims && dims.rows) ? dims.rows : (it.term && it.term.rows ? it.term.rows : 0);
      // 某些场景（返回页面/切 tab）xterm viewport 会卡住不刷新，refresh 一下强制重绘。
      try{ if(it.term && typeof it.term.refresh === 'function' && it.term.rows) it.term.refresh(0, it.term.rows - 1); }catch(_){ }
      return { cols, rows };
    }

    function _sendResizeIfOnline(it, dims){
      if(!it) return;
      if(!it.ws || it.ws.readyState !== 1){
        it.__needs_resize = true;
        return;
      }
      const cols = (dims && dims.cols) ? dims.cols : (it.term && it.term.cols ? it.term.cols : 0);
      const rows = (dims && dims.rows) ? dims.rows : (it.term && it.term.rows ? it.term.rows : 0);
      if(cols > 0 && rows > 0){
        try{ it.ws.send(JSON.stringify({ type: 'resize', cols, rows })); }catch(_){ }
        it.__needs_resize = false;
      }
    }

    function _fitAndMaybeResize(it){
      if(!it || !it.viewEl) return;
      if(it.viewEl.classList && it.viewEl.classList.contains('rt-hidden')) return;
      const dims = _doFit(it);
      if(it.__needs_resize || (it.ws && it.ws.readyState === 1)){
        _sendResizeIfOnline(it, dims);
      }
    }

    function scheduleFit(it){
      if(!it) return;
      // 清理上一次计划，避免抖动时堆积。
      try{
        if(it.__fit_raf1) cancelAnimationFrame(it.__fit_raf1);
        if(it.__fit_raf2) cancelAnimationFrame(it.__fit_raf2);
      }catch(_){ }
      try{ if(it.__fit_timer) clearTimeout(it.__fit_timer); }catch(_){ }
      it.__fit_raf1 = requestAnimationFrame(()=>{
        it.__fit_raf2 = requestAnimationFrame(()=>{ _fitAndMaybeResize(it); });
      });
      // 再补一枪：等布局/字体稳定后再次 fit（解决“只占一小块/无法滚动”）。
      it.__fit_timer = setTimeout(()=>{ _fitAndMaybeResize(it); }, 120);
    }

    async function ensureHistoryLoaded(it){
      if(!it) return;
      if(it.__history_loaded) return;
      if(it.__history_loading) return;
      it.__history_loading = true;
      try{
        // 必须在 view 可见且 fit 过之后再回放 history。
        // 否则会用默认 80x24 回放，导致 coco 这类 TUI 的换行/光标定位彻底错乱。
        await new Promise((resolve)=>{
          requestAnimationFrame(()=>{
            requestAnimationFrame(()=>{
              setTimeout(resolve, 60);
            });
          });
        });
        try{ _doFit(it); }catch(_){ }
        await loadHistory(it);
        it.__history_loaded = true;
      }catch(_){
        // best-effort
      }finally{
        it.__history_loading = false;
        try{ scheduleFit(it); }catch(_){ }
      }
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
        scheduleFit(it);
        focusActive();
        applyAgentUi();

        // 先在正确尺寸下回放 history，再连接 WS 接收实时输出。
        void (async ()=>{
          await ensureHistoryLoaded(it);
          if(!it.__ws_connect) attachWs(it);
        })();
      }
    }

    function attachWs(it){
      const tid = String(it.terminalId||'');
      const url = wsUrl(`/api/agent_spaces/${spaceId}/terminals/${encodeURIComponent(tid)}/ws`);
      // 关键：必须使用流式解码，否则 UTF-8 多字节字符（如 box-drawing: '─'）跨 WS 帧/分片时会出现乱码。
      // TextDecoder 支持通过 { stream: true } 累积半个字符的 bytes。
      it.decoder = new TextDecoder('utf-8');
      it.__closing = false;
      it.__reconnect_attempt = 0;
      it.__reconnect_timer = null;
      it.__keepalive_timer = null;
      it.__last_status = '';
      it.__last_status_at = 0;

      const clearTimers = ()=>{
        if(it.__reconnect_timer){ try{ clearTimeout(it.__reconnect_timer); }catch(_){} it.__reconnect_timer = null; }
        if(it.__keepalive_timer){ try{ clearInterval(it.__keepalive_timer); }catch(_){} it.__keepalive_timer = null; }
      };

      const setConnStatus = (s)=>{
        const now = Date.now();
        const msg = String(s||'');
        // 防止状态刷屏（至少间隔 1200ms）
        if(it.__last_status === msg && (now - (it.__last_status_at||0)) < 1200) return;
        it.__last_status = msg;
        it.__last_status_at = now;
        setStatus(msg);
      };

      const connect = ()=>{
        if(it.__closing) return;
        try{ if(it.ws && (it.ws.readyState === 0 || it.ws.readyState === 1)) return; }catch(_){ }

        const ws = new WebSocket(url);
        it.ws = ws;

        ws.onopen = ()=>{
          it.__reconnect_attempt = 0;
          setConnStatus('connected');
          it.__needs_resize = true;
          scheduleFit(it);

          // keepalive：避免中间层 idle timeout（服务端会忽略未知 type）
          clearTimers();
          it.__keepalive_timer = setInterval(()=>{
            try{
              if(!it.ws || it.ws.readyState !== 1) return;
              it.ws.send(JSON.stringify({ type: 'ping', ts: Date.now() }));
            }catch(_){ }
          }, 25000);
        };

        ws.onmessage = (ev)=>{
          try{
            const msg = JSON.parse(ev.data||'{}');
            if(msg && msg.type === 'output'){
              const u8 = b64ToBytes(msg.data_b64 || '');
              // 流式 decode：修复“横线/框线字符在 resize 后变成 � 或乱码”的问题。
              const text = it.decoder.decode(u8, { stream: true });
              if(text) it.term.write(text);
              if(msg.error){ it.term.write(`\r\n\x1b[31m[error]\x1b[0m ${String(msg.error)}\r\n`); }
              if(msg.closed){
                // flush decoder buffer
                try{
                  const tail = it.decoder.decode(new Uint8Array());
                  if(tail) it.term.write(tail);
                }catch(_){ }
                it.term.write(`\r\n\x1b[90m[closed]\x1b[0m\r\n`);
              }
            }
            if(msg && msg.type === 'error'){
              // 服务端明确告知 terminal 不可用
              setConnStatus('terminal unavailable');
              it.term.write(`\r\n\x1b[31m[terminal unavailable]\x1b[0m ${String(msg.error||'')}\r\n`);
            }
          }catch(_){ }
        };

        ws.onerror = ()=>{
          // onclose 会统一触发重连
          it.term.write(`\r\n\x1b[31m[ws error]\x1b[0m\r\n`);
        };

        ws.onclose = (ev)=>{
          clearTimers();
          // flush decoder buffer on close; then reset decoder for next reconnect.
          try{
            if(it.decoder){
              const tail = it.decoder.decode(new Uint8Array());
              if(tail) it.term.write(tail);
            }
          }catch(_){ }
          try{ it.decoder = new TextDecoder('utf-8'); }catch(_){ }
          if(it.__closing) {
            setConnStatus('disconnected');
            return;
          }
          const closeCode = (ev && typeof ev.code === 'number') ? ev.code : 0;
          // 1008 / 4404 表示“终端不可用/不允许”，无需重连，等待用户新建。
          if(closeCode === 1008 || closeCode === 4404){
            setConnStatus('terminal unavailable');
            return;
          }

          setConnStatus('disconnected, retrying');

          // 指数退避重连（上限 10s；Companion 离线用更慢的重试）
          it.__reconnect_attempt = (it.__reconnect_attempt || 0) + 1;
          const slow = (closeCode === 1013);
          const base = slow
            ? Math.min(60000, 2000 * Math.pow(2, Math.min(6, it.__reconnect_attempt - 1)))
            : Math.min(10000, 500 * Math.pow(2, Math.min(6, it.__reconnect_attempt - 1)));
          const jitter = Math.floor(Math.random() * 200);
          const waitMs = base + jitter;
          it.__reconnect_timer = setTimeout(connect, waitMs);
        };
      };

      it.__ws_connect = connect;
      it.__ws_clear = clearTimers;
      connect();
    }

    async function loadHistory(it){
      const tid = String(it && it.terminalId ? it.terminalId : '');
      if(!tid) return;
      try{
        // 对 TUI（coco）更友好：回放窗口加大，服务端也会尝试从“可重建屏幕”的同步点开始切片。
        const data = await fetchJson(`/api/agent_spaces/${spaceId}/terminals/${encodeURIComponent(tid)}/history?max_bytes=${encodeURIComponent(String(4*1024*1024))}`);
        const b64 = String(data && data.data_b64 ? data.data_b64 : '');
        if(!b64) return;
        const u8 = b64ToBytes(b64);
        // 回放前先 reset，避免混入默认状态/残留。
        try{ if(it.term && typeof it.term.reset === 'function') it.term.reset(); }catch(_){ }
        // history decode 使用新的 decoder（不要复用 ws 的 stream decoder 状态）。
        const text = (new TextDecoder('utf-8')).decode(u8);
        if(text) it.term.write(text);
        if(data && data.truncated){
          it.term.write(`\r\n\x1b[90m[history truncated]\x1b[0m\r\n`);
        }
      }catch(_){
        // history is best-effort
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
      if(!tid || terminals.has(tid)) return;

      const cfg = opts || {};
      const connectWs = (cfg.connectWs !== false);

      const tab = document.createElement('div');
      tab.className = 'rt-tab';
      const nm = normalizeName(name) || shortId(tid);
      tab.innerHTML = `<span class="rt-name" title="Double click to rename">${esc(nm)}</span><span class="rt-x" title="Close">×</span>`;
      tabsEl.appendChild(tab);
      const nameEl = tab.querySelector('.rt-name');

      const view = document.createElement('div');
      view.className = 'rt-term rt-hidden';
      bodyEl.appendChild(view);

      const { term, fit } = createTerminal();
      term.open(view);

      const it = { terminalId: tid, name: nm, tabEl: tab, nameEl, viewEl: view, term, fit, ws: null, decoder: null };
      it.__needs_resize = true;
      it.__agent_mode = loadAgentMode(tid);
      it.__history_loaded = false;
      it.__history_loading = false;
      terminals.set(tid, it);

      // input -> WS (base64)
      const enc = new TextEncoder();
      term.onData((data)=>{
        if(!it.ws || it.ws.readyState !== 1) return;
        const s = String(data||'');

        // AGENT 模式：在用户“按下回车提交”时，将系统提示词追加到本次输入的末尾（紧贴在 '\r' 之前）。
        // - 按 terminal 生效：it.__agent_mode
        // - Ctrl+C 不追加
        if(it.__agent_mode){
          if(isCtrlC(s)){
            sendInputBytes(it, enc.encode(s));
            return;
          }
          if(s.indexOf('\r') >= 0){
            const sys = buildAgentPrefix();
            // 用 bracketed paste 包裹，尽量减少对 TUI 的破坏性
            const pasted = `\x1b[200~ ${sys}\x1b[201~`;
            const out = s.replace(/\r/g, pasted + '\r');
            sendInputBytes(it, enc.encode(out));
            return;
          }
        }

        sendInputBytes(it, enc.encode(s));
      });

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

      if(connectWs) attachWs(it);
      return it;
    }

    async function closeTerminal(terminalId){
      const tid = String(terminalId||'');
      const it = terminals.get(tid);
      if(!it) return;
      it.__closing = true;
      try{ if(typeof it.__ws_clear === 'function') it.__ws_clear(); }catch(_){ }
      try{
        await fetchJson(`/api/agent_spaces/${spaceId}/terminals/${encodeURIComponent(tid)}/close`, { method: 'POST' });
      }catch(e){
        it.__closing = false;
        try{ toast('关闭失败'); }catch(_){ }
        alert('关闭失败：' + String(e && e.message ? e.message : e));
        return;
      }

      try{ if(it.ws) it.ws.close(); }catch(_){ }
      try{ it.term.dispose(); }catch(_){ }
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
        addTab(tid, name);
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
        // 注意：不要在这里回放 history（此时 view 还没显示/没 fit，会导致 TUI 换行错乱）。
        // WS 也延迟到 activate 后再连：先回放，再接实时输出。
        addTab(tid, name, { connectWs: false });
      }
      if(arr.length){
        activate(String(arr[0].terminal_id||''));
        const it0 = terminals.get(String(arr[0].terminal_id||''));
        if(it0) scheduleFit(it0);
      }else if(online){
        await createNew();
      }
    }

    btnNew?.addEventListener('click', createNew);

    agentSwitch?.addEventListener('change', ()=>{
      const it = terminals.get(activeId);
      if(!it){
        if(agentSwitch && agentSwitch instanceof HTMLInputElement) agentSwitch.checked = false;
        return;
      }
      it.__agent_mode = !!(agentSwitch && agentSwitch instanceof HTMLInputElement && agentSwitch.checked);
      saveAgentMode(it.terminalId, it.__agent_mode);
      applyAgentUi();
      try{ toast(it.__agent_mode ? 'Agent Mode: ON' : 'Agent Mode: OFF'); }catch(_){ }
      focusActive();
    });
    window.addEventListener('resize', ()=>{
      const it = terminals.get(activeId);
      if(it) scheduleFit(it);
    });
    const ro = (window.ResizeObserver ? new ResizeObserver(()=>{
      const it = terminals.get(activeId);
      if(it) scheduleFit(it);
    }) : null);
    if(ro && bodyEl) ro.observe(bodyEl);

    // 返回页面/从后台切回时，重新计算尺寸，避免 xterm 卡住。
    window.addEventListener('pageshow', ()=>{
      const it = terminals.get(activeId);
      if(it) scheduleFit(it);
    });
    document.addEventListener('visibilitychange', ()=>{
      if(document.visibilityState !== 'visible') return;
      const it = terminals.get(activeId);
      if(it) scheduleFit(it);
    });
    try{
      if(document.fonts && document.fonts.ready){
        document.fonts.ready.then(()=>{
          const it = terminals.get(activeId);
          if(it) scheduleFit(it);
        }).catch(()=>{});
      }
    }catch(_){ }

    loadExisting();

    applyAgentUi();

    return {
      createNew,
      closeTerminal,
      activate,
    };
  }

  window.OpenFocusRemoteTerminal = { mount };
})();

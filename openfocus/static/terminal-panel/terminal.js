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

  const AGENT_SPACE_SETTINGS_KEY = 'openfocus.agent_space.settings.v1';
  const AGENT_SPACE_SETTINGS_EVENT = 'openfocus:agent-space-settings-changed';
  const AGENT_SPACE_SHORTCUTS_KEY = 'openfocus.agent_space.shortcuts.v1';
  const AGENT_SPACE_SHORTCUTS_EVENT = 'openfocus:agent-space-shortcuts-changed';
  const AGENT_SPACE_DEFAULT_SETTINGS = {
    filesFontSize: 13,
    previewFontSize: 12,
    terminalFontSize: 13,
    showFiles: true,
    showPreview: true,
    showTerminal: true,
  };
  const AGENT_SPACE_SHORTCUT_COMMANDS = [
    { id: 'search_everywhere', label: 'Search Everywhere', scope: 'global except active terminal input', defaults: [{ keys: ['Shift', 'Shift'], platform: 'all' }] },
    { id: 'find_in_files', label: 'Find in Files', scope: 'global except active terminal input', defaults: [{ keys: ['Meta', 'Shift', 'F'], platform: 'mac' }, { keys: ['Ctrl', 'Shift', 'F'], platform: 'other' }] },
    { id: 'go_to_definition', label: 'Go to Definition', scope: 'PREVIEW', defaults: [{ keys: ['Meta', 'B'], platform: 'mac' }, { keys: ['Ctrl', 'B'], platform: 'other' }] },
    { id: 'find_usages', label: 'Find Usages', scope: 'PREVIEW', defaults: [{ keys: ['Alt', 'F7'], platform: 'all' }] },
    { id: 'navigation_back', label: 'Navigate Back', scope: 'AgentSpace', defaults: [{ keys: ['Meta', '['], platform: 'mac' }, { keys: ['Ctrl', 'Alt', 'ArrowLeft'], platform: 'other' }] },
    { id: 'navigation_forward', label: 'Navigate Forward', scope: 'AgentSpace', defaults: [{ keys: ['Meta', ']'], platform: 'mac' }, { keys: ['Ctrl', 'Alt', 'ArrowRight'], platform: 'other' }] },
    { id: 'focus_files', label: 'Focus Files', scope: 'AgentSpace', defaults: [{ keys: ['Alt', '1'], platform: 'all' }] },
    { id: 'focus_preview', label: 'Focus Preview', scope: 'AgentSpace', defaults: [{ keys: ['Alt', '2'], platform: 'all' }] },
    { id: 'focus_terminal', label: 'Focus Terminal', scope: 'AgentSpace', defaults: [{ keys: ['Alt', '3'], platform: 'all' }] },
  ];
  const AGENT_SPACE_SHORTCUT_COMMAND_BY_ID = new Map(AGENT_SPACE_SHORTCUT_COMMANDS.map((cmd)=> [cmd.id, cmd]));
  const AGENT_SPACE_SHORTCUT_DEFAULTS = {
    version: 1,
    bindings: Object.fromEntries(AGENT_SPACE_SHORTCUT_COMMANDS.map((cmd)=> [cmd.id, cmd.defaults.map((binding)=> ({ keys: binding.keys.slice(), platform: binding.platform }))])),
  };
  const MODIFIER_KEYS = ['Meta', 'Ctrl', 'Alt', 'Shift'];

  function clampNumber(value, minValue, maxValue, fallback){
    const n = Number(value);
    if(!Number.isFinite(n)) return fallback;
    return Math.max(minValue, Math.min(maxValue, Math.round(n)));
  }

  function normalizeAgentSpaceSettings(raw){
    const src = raw && typeof raw === 'object' ? raw : {};
    return {
      filesFontSize: clampNumber(src.filesFontSize, 10, 24, AGENT_SPACE_DEFAULT_SETTINGS.filesFontSize),
      previewFontSize: clampNumber(src.previewFontSize, 10, 24, AGENT_SPACE_DEFAULT_SETTINGS.previewFontSize),
      terminalFontSize: clampNumber(src.terminalFontSize, 10, 24, AGENT_SPACE_DEFAULT_SETTINGS.terminalFontSize),
      showFiles: src.showFiles !== false,
      showPreview: src.showPreview !== false,
      showTerminal: src.showTerminal !== false,
    };
  }

  function loadAgentSpaceSettings(){
    try{
      const raw = localStorage.getItem(AGENT_SPACE_SETTINGS_KEY);
      return normalizeAgentSpaceSettings(raw ? JSON.parse(raw) : {});
    }catch(_){
      return normalizeAgentSpaceSettings({});
    }
  }

  function saveAgentSpaceSettings(settings, source){
    const next = normalizeAgentSpaceSettings(settings);
    try{ localStorage.setItem(AGENT_SPACE_SETTINGS_KEY, JSON.stringify(next)); }catch(_){ }
    try{ window.dispatchEvent(new CustomEvent(AGENT_SPACE_SETTINGS_EVENT, { detail: { settings: next, source: source || 'terminal' } })); }catch(_){ }
    return next;
  }

  function currentShortcutPlatform(){
    try{
      const p = String(navigator && (navigator.userAgentData && navigator.userAgentData.platform || navigator.platform || '') || '').toLowerCase();
      return /mac|iphone|ipad|ipod/.test(p) ? 'mac' : 'other';
    }catch(_){
      return 'other';
    }
  }

  function normalizeShortcutKeyName(key){
    const raw = String(key || '').trim();
    const lower = raw.toLowerCase();
    if(!raw) return '';
    if(lower === 'cmd' || lower === 'command' || lower === 'meta' || lower === 'os') return 'Meta';
    if(lower === 'control' || lower === 'ctrl') return 'Ctrl';
    if(lower === 'option' || lower === 'alt') return 'Alt';
    if(lower === 'shift') return 'Shift';
    if(lower === 'esc' || lower === 'escape') return 'Escape';
    if(lower === 'space' || lower === 'spacebar' || raw === ' ') return 'Space';
    if(lower === 'left' || lower === 'arrowleft') return 'ArrowLeft';
    if(lower === 'right' || lower === 'arrowright') return 'ArrowRight';
    if(lower === 'up' || lower === 'arrowup') return 'ArrowUp';
    if(lower === 'down' || lower === 'arrowdown') return 'ArrowDown';
    if(/^key[a-z]$/i.test(raw)) return raw.slice(3).toUpperCase();
    if(/^digit[0-9]$/i.test(raw)) return raw.slice(5);
    if(/^f([1-9]|1[0-9]|2[0-4])$/i.test(raw)) return raw.toUpperCase();
    if(raw.length === 1 && /^[a-z]$/i.test(raw)) return raw.toUpperCase();
    return raw;
  }

  function normalizeShortcutBinding(raw){
    if(!raw || typeof raw !== 'object' || !Array.isArray(raw.keys)) return null;
    const keys = raw.keys.map(normalizeShortcutKeyName).filter(Boolean);
    const platform = raw.platform === 'mac' || raw.platform === 'other' || raw.platform === 'all' ? raw.platform : 'all';
    if(!keys.length) return null;
    if(keys.length === 2 && keys[0] === 'Shift' && keys[1] === 'Shift') return { keys: ['Shift', 'Shift'], platform };
    const out = [];
    MODIFIER_KEYS.forEach((k)=> { if(keys.includes(k)) out.push(k); });
    keys.forEach((k)=> { if(!MODIFIER_KEYS.includes(k) && !out.includes(k)) out.push(k); });
    return { keys: out, platform };
  }

  function normalizeShortcutSettings(raw){
    const src = raw && typeof raw === 'object' ? raw : {};
    const rawBindings = src.bindings && typeof src.bindings === 'object' ? src.bindings : {};
    const bindings = {};
    AGENT_SPACE_SHORTCUT_COMMANDS.forEach((cmd)=> {
      const value = rawBindings[cmd.id];
      bindings[cmd.id] = Array.isArray(value)
        ? value.map(normalizeShortcutBinding).filter(Boolean)
        : AGENT_SPACE_SHORTCUT_DEFAULTS.bindings[cmd.id].map((binding)=> ({ keys: binding.keys.slice(), platform: binding.platform }));
    });
    return { version: 1, bindings };
  }

  function loadAgentSpaceShortcuts(){
    try{
      const raw = localStorage.getItem(AGENT_SPACE_SHORTCUTS_KEY);
      return normalizeShortcutSettings(raw ? JSON.parse(raw) : null);
    }catch(_){
      return normalizeShortcutSettings(null);
    }
  }

  function saveAgentSpaceShortcuts(settings, source){
    const next = normalizeShortcutSettings(settings);
    try{ localStorage.setItem(AGENT_SPACE_SHORTCUTS_KEY, JSON.stringify(next)); }catch(_){ }
    try{ window.dispatchEvent(new CustomEvent(AGENT_SPACE_SHORTCUTS_EVENT, { detail: { shortcuts: next, source: source || 'terminal' } })); }catch(_){ }
    return next;
  }

  function shortcutPlatformMatches(bindingPlatform, platform){
    return bindingPlatform === 'all' || platform === 'all' || bindingPlatform === platform;
  }

  function shortcutPlatformsOverlap(left, right){
    return left === 'all' || right === 'all' || left === right;
  }

  function resolveShortcutBinding(settings, commandId, platform){
    const bindings = normalizeShortcutSettings(settings).bindings[commandId] || [];
    return bindings.find((binding)=> binding.platform === platform) || bindings.find((binding)=> shortcutPlatformMatches(binding.platform, platform)) || null;
  }

  function formatShortcutBinding(binding, platform){
    const normalized = normalizeShortcutBinding(binding);
    if(!normalized) return 'Unassigned';
    if(normalized.keys.length === 2 && normalized.keys[0] === 'Shift' && normalized.keys[1] === 'Shift') return 'Double Shift';
    return normalized.keys.map((key)=> {
      if(key === 'Meta') return platform === 'mac' ? 'Cmd' : 'Meta';
      if(key === 'ArrowLeft') return 'Left';
      if(key === 'ArrowRight') return 'Right';
      if(key === 'ArrowUp') return 'Up';
      if(key === 'ArrowDown') return 'Down';
      return key;
    }).join('+');
  }

  function normalizeShortcutKeyEvent(event){
    if(!event) return null;
    const code = String(event.code || '');
    const baseKey = normalizeShortcutKeyName(/^Key[A-Z]$|^Digit[0-9]$/i.test(code) ? code : event.key);
    if(!baseKey) return null;
    const keys = [];
    if(event.metaKey) keys.push('Meta');
    if(event.ctrlKey) keys.push('Ctrl');
    if(event.altKey) keys.push('Alt');
    if(event.shiftKey) keys.push('Shift');
    if(!MODIFIER_KEYS.includes(baseKey)) keys.push(baseKey);
    return normalizeShortcutBinding({ keys, platform: 'all' });
  }

  function shortcutConflict(settings, commandId, binding){
    const normalized = normalizeShortcutBinding(binding);
    if(!normalized) return null;
    const signature = normalized.keys.join('+');
    const all = normalizeShortcutSettings(settings);
    for(const cmd of AGENT_SPACE_SHORTCUT_COMMANDS){
      if(cmd.id === commandId) continue;
      for(const other of all.bindings[cmd.id] || []){
        if(other.keys.join('+') === signature && shortcutPlatformsOverlap(other.platform, normalized.platform)){
          return { commandId: cmd.id, label: cmd.label, binding: other };
        }
      }
    }
    return null;
  }

  function validateShortcutBinding(binding, settings, commandId){
    const normalized = normalizeShortcutBinding(binding);
    if(!normalized) return { ok: false, reason: 'empty' };
    const baseKey = normalized.keys[normalized.keys.length - 1];
    const modifierCount = normalized.keys.slice(0, -1).filter((key)=> MODIFIER_KEYS.includes(key)).length;
    if(['L', 'R', 'W', 'T', 'N'].includes(baseKey) && modifierCount === 1 && (normalized.keys.includes('Meta') || normalized.keys.includes('Ctrl'))){
      return { ok: false, reason: 'reserved_browser_shortcut' };
    }
    const isDoubleShift = normalized.keys.length === 2 && normalized.keys[0] === 'Shift' && normalized.keys[1] === 'Shift';
    const hasModifier = normalized.keys.some((key)=> MODIFIER_KEYS.includes(key));
    const hasNonModifier = normalized.keys.some((key)=> !MODIFIER_KEYS.includes(key));
    const isFunction = /^F([1-9]|1[0-9]|2[0-4])$/.test(baseKey);
    if(!isDoubleShift && !hasNonModifier) return { ok: false, reason: 'plain_key_without_modifier' };
    if(!isDoubleShift && !hasModifier && !isFunction) return { ok: false, reason: 'plain_key_without_modifier' };
    const conflict = settings && commandId ? shortcutConflict(settings, commandId, normalized) : null;
    if(conflict) return { ok: false, reason: 'conflict', conflict };
    return { ok: true, binding: normalized };
  }

  function formatElapsedFrom(iso){
    const raw = String(iso || '').trim();
    if(!raw) return '—';
    const d = new Date(raw);
    if(Number.isNaN(d.getTime())) return '—';
    const sec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if(sec < 60) return `${sec}s`;
    const min = Math.floor(sec / 60);
    if(min < 60) return `${min}m`;
    const hours = Math.floor(min / 60);
    if(hours < 48) return `${hours}h ${min % 60}m`;
    const days = Math.floor(hours / 24);
    return `${days}d ${hours % 24}h`;
  }

  function mount(rootEl, opts){
    const spaceId = Number(opts && opts.spaceId ? opts.spaceId : 0);
    const taskPublicId = String(opts && opts.taskPublicId ? opts.taskPublicId : '').trim();
    const apiBase = String(opts && opts.apiBase ? opts.apiBase : `/api/agent_spaces/${spaceId}/terminals`).replace(/\/+$/, '');
    const mode = String(opts && opts.mode ? opts.mode : 'agent_space');
    const isInspiration = mode === 'inspiration';
    const commandApi = String(opts && opts.commandApi ? opts.commandApi : `/api/agent_spaces/${spaceId}/start_agent_command`).replace(/\/+$/, '');
    const promptApi = String(opts && opts.promptApi ? opts.promptApi : '/api/agent_space_prompts').replace(/\/+$/, '');
    const taskBasic = String(opts && opts.taskBasic ? opts.taskBasic : '');
    const taskTitle = String(opts && opts.taskTitle ? opts.taskTitle : 'Untitled task').trim() || 'Untitled task';
    const taskUrl = String(opts && opts.taskUrl ? opts.taskUrl : (taskPublicId ? `/goals?task=${encodeURIComponent(taskPublicId)}` : '')).trim();
    const taskDueDate = String(opts && opts.taskDueDate ? opts.taskDueDate : '').trim();
    const spaceCompanion = String(opts && opts.spaceCompanion ? opts.spaceCompanion : '').trim();
    const spaceCreatedAt = String(opts && opts.spaceCreatedAt ? opts.spaceCreatedAt : '').trim();
    let startAgentCommand = String(opts && opts.startAgentCommand ? opts.startAgentCommand : '').trim();
    let autoStartDefaultTerminalPending = !!(opts && opts.autoStartDefaultTerminal) && !!startAgentCommand;
    let customPrompts = [];
    const goalResources = Array.isArray(opts && opts.goalResources)
      ? opts.goalResources.map((r)=> ({ id: Number(r && r.id ? r.id : 0), title: String(r && r.title ? r.title : '') })).filter((r)=> r.id && r.title)
      : [];
    const goalSelectOptionsHtml = goalResources.length
      ? '<option value="">Choose a resource…</option>' + goalResources.map((r)=> `<option value="${esc(r.title)}">${esc(r.title)}</option>`).join('')
      : '<option value="">No resources available</option>';
    if(!rootEl) throw new Error('mount element required');
    if(!spaceId) throw new Error('spaceId required');
    const sideRootEl = opts && opts.sideRoot instanceof HTMLElement ? opts.sideRoot : null;
    const q = (sel)=> $(sel, rootEl) || (sideRootEl ? $(sel, sideRootEl) : null);
    const qq = (sel)=> [
      ...Array.from(rootEl.querySelectorAll(sel)),
      ...(sideRootEl ? Array.from(sideRootEl.querySelectorAll(sel)) : []),
    ];

    function mouseModeKey(terminalId){
      const tid = String(terminalId || '').trim();
      return `openfocus.${mode}.terminal.mouse_mode.${String(spaceId)}.${tid}`;
    }

    function builtinAutoPromptKey(key){
      return `openfocus.${mode}.prompt.auto_builtin.${String(key || '').trim()}`;
    }

    function loadBuiltinAutoPrompt(key){
      try{ return (localStorage.getItem(builtinAutoPromptKey(key)) || '') === '1'; }catch(_){ return false; }
    }

    function saveBuiltinAutoPrompt(key, v){
      try{ localStorage.setItem(builtinAutoPromptKey(key), v ? '1' : '0'); }catch(_){ }
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
      parts.push('重要进展同步: POST /api/agent/events kind=task.progress; 步骤启动/完成或长期任务每约5分钟同步一次; 不要求启动/结束/成功/失败上报');
      return parts.join(' · ');
    }

    function buildPasteText(kind){
      const prefix = buildAgentPrefix();
      const k = String(kind || 'context');
      if(k === 'draft_summary') return `[OpenFocus Summary Request] ${String(opts && opts.draftSummaryPrompt ? opts.draftSummaryPrompt : '')}`;
      if(k === 'task_basic') return taskBasic;
      if(k === 'report_progress') return `[OpenFocus Report Progress]\n${prefix}`;
      if(k === 'pua') return PUA_PROACTIVITY_PROMPT;
      return prefix;
    }

    function normalizeAutoPromptText(text){
      return String(text || '').replace(/\s+/g, ' ').trim();
    }

    const taskLinkHtml = taskUrl
      ? `<a href="${esc(taskUrl)}" class="rt-task-title" title="${esc(taskTitle)}">${esc(taskTitle)}</a>`
      : `<span class="rt-task-title" title="${esc(taskTitle)}">${esc(taskTitle)}</span>`;
    const paneIconHtml = (pane, label)=> `<button type="button" class="rt-pane-icon" data-rt-pane="${esc(pane)}" aria-label="${esc(label)}" title="${esc(label)}" aria-pressed="true">${esc(label[0] || '')}</button>`;
    const taskPanelHtml = isInspiration ? '' : `
          <div class="rt-task-panel rt-settings-panel">
            <div class="rt-settings-title-row">
              ${taskLinkHtml}
              <button type="button" class="btn-ghost rt-task-show" id="rt-task-show">show</button>
            </div>
            <div class="rt-settings-control-row">
              <div class="rt-pane-icons" aria-label="AgentSpace panes">
                ${paneIconHtml('files', 'Files')}
                ${paneIconHtml('preview', 'Preview')}
                ${paneIconHtml('terminal', 'Terminal')}
              </div>
              <button type="button" class="btn-ghost rt-start-agent-edit" id="rt-start-agent-edit" title="edit start agent command" aria-label="edit start agent command">✏</button>
            </div>
          </div>`;
    const taskDetailsModalHtml = isInspiration ? '' : `
        <div class="rt-modal-backdrop" id="rt-task-details-modal" hidden>
          <div class="rt-modal-card rt-task-modal-card">
            <div class="rt-modal-head">
              <strong>Task Details</strong>
              <button type="button" class="btn-ghost" id="rt-task-details-x">×</button>
            </div>
            <div class="rt-modal-body">
              <div class="rt-detail-row"><div class="rt-detail-label">Title</div><div class="rt-detail-value">${taskLinkHtml}</div></div>
              <div class="rt-detail-row"><div class="rt-detail-label">Basic</div><div class="rt-detail-value rt-task-basic">${esc(taskBasic || '—')}</div></div>
              <div class="rt-detail-row"><div class="rt-detail-label">Companion</div><div class="rt-detail-value">${esc(spaceCompanion || '—')}</div></div>
              <div class="rt-detail-row"><div class="rt-detail-label">Elapsed</div><div class="rt-detail-value" id="rt-task-elapsed">${esc(formatElapsedFrom(spaceCreatedAt))}</div></div>
              <div class="rt-detail-row"><div class="rt-detail-label">DDL</div><div class="rt-detail-value">${esc(taskDueDate || '—')}</div></div>
            </div>
            <div class="rt-modal-actions">
              ${taskUrl ? `<a class="btn-primary rt-modal-link" href="${esc(taskUrl)}" role="button">Goto Task</a>` : ''}
              <button type="button" class="btn-danger" id="rt-task-cleanup">Cleanup</button>
            </div>
          </div>
        </div>`;
    const startSettingsModalHtml = isInspiration ? '' : `
        <div class="rt-modal-backdrop" id="rt-start-settings-modal" hidden>
          <div class="rt-modal-card rt-settings-modal-card">
            <div class="rt-modal-head">
              <strong>AgentSpace Settings</strong>
              <button type="button" class="btn-ghost" id="rt-start-settings-x">×</button>
            </div>
            <div class="rt-modal-body">
              <div class="rt-settings-tabs" role="tablist" aria-label="AgentSpace settings sections">
                <button type="button" class="rt-settings-tab active" data-rt-settings-tab="general" role="tab" aria-selected="true">General</button>
                <button type="button" class="rt-settings-tab" data-rt-settings-tab="appearance" role="tab" aria-selected="false">Appearance</button>
                <button type="button" class="rt-settings-tab" data-rt-settings-tab="shortcuts" role="tab" aria-selected="false">Shortcuts</button>
              </div>
              <div class="rt-settings-tab-panel" data-rt-settings-panel="general">
                <label class="rt-settings-field" for="rt-start-command-input">
                  <span>Start Agent command</span>
                  <textarea id="rt-start-command-input" rows="3" placeholder="coco -y"></textarea>
                </label>
                <div class="rt-pane-toggles" aria-label="AgentSpace panes">
                  <label><input id="rt-show-files" type="checkbox" /> <span>files</span></label>
                  <label><input id="rt-show-preview" type="checkbox" /> <span>preview</span></label>
                  <label><input id="rt-show-terminal" type="checkbox" /> <span>terminal</span></label>
                </div>
              </div>
              <div class="rt-settings-tab-panel" data-rt-settings-panel="appearance" hidden>
                <div class="rt-settings-grid">
                  <label class="rt-settings-field" for="rt-files-font-size"><span>Files font</span><input id="rt-files-font-size" type="number" min="10" max="24" step="1" /></label>
                  <label class="rt-settings-field" for="rt-preview-font-size"><span>Preview font</span><input id="rt-preview-font-size" type="number" min="10" max="24" step="1" /></label>
                  <label class="rt-settings-field" for="rt-terminal-font-size"><span>Terminal font</span><input id="rt-terminal-font-size" type="number" min="10" max="24" step="1" /></label>
                </div>
              </div>
              <div class="rt-settings-tab-panel" data-rt-settings-panel="shortcuts" hidden>
                <div class="rt-shortcut-list" id="rt-shortcut-list" aria-label="AgentSpace shortcuts"></div>
                <div class="rt-shortcut-status" id="rt-shortcut-status" aria-live="polite"></div>
              </div>
            </div>
            <div class="rt-modal-actions">
              <button type="button" class="btn-ghost" id="rt-start-settings-cancel">Cancel</button>
              <button type="button" class="btn-primary" id="rt-start-settings-save">Save</button>
            </div>
          </div>
        </div>`;

    const terminalWrapHtml = `
        <div class="rt-wrap">
          <div class="rt-top">
            <div class="rt-tabs" id="rt-tabs"></div>
            <div class="rt-actions">
              <div class="rt-status" id="rt-status">—</div>
              <button type="button" class="btn-ghost" id="rt-new" title="New terminal">+</button>
            </div>
          </div>
          <div class="rt-body" id="rt-body"></div>
        </div>`;
    const sideHtml = `
        <div class="rt-side">
          ${taskPanelHtml}
          <div class="rt-prompt-zone" id="rt-prompt-zone">
            <div class="rt-side-title">prompt zone</div>
            <label class="rt-agent-switch rt-mouse-switch" title="scroll: wheel scrolls tmux history. copy: browser drag-copy friendly."><input type="checkbox" id="rt-mouse-switch" /><span class="rt-agent-slider" aria-hidden="true"></span><span class="rt-agent-text" id="rt-mouse-text">scroll</span></label>
            ${isInspiration ? '<button type="button" class="btn-ghost" id="rt-draft-summary" title="send the summary instructions as plain text into this terminal without pressing enter.">summary</button><button type="button" class="btn-primary insp-create-btn" id="rt-create-goal" style="margin-top:auto;" title="choose a resource and generate a reviewable goal/tasks draft from it.">create goal</button>' : '<div class="rt-zone-divider" aria-hidden="true"></div><div class="rt-zone-section"><div class="rt-prompt-row"><button type="button" class="btn-ghost rt-prompt-main" id="rt-send-basic" title="send the task Basic content into the active terminal without pressing enter.">send basic</button><label class="rt-auto-switch" title="append this prompt whenever a message is submitted"><input type="checkbox" data-auto-builtin="task_basic" /><span>auto</span></label></div><div class="rt-prompt-row"><button type="button" class="btn-ghost rt-prompt-main" id="rt-report-progress">report progress</button><label class="rt-auto-switch" title="append this prompt whenever a message is submitted"><input type="checkbox" data-auto-builtin="report_progress" /><span>auto</span></label></div><div class="rt-prompt-row"><button type="button" class="btn-ghost rt-prompt-main" id="rt-pua" title="inject a proactivity escalation prompt into the active terminal.">pua</button><label class="rt-auto-switch" title="append this prompt whenever a message is submitted"><input type="checkbox" data-auto-builtin="pua" /><span>auto</span></label></div></div><div class="rt-zone-divider" aria-hidden="true"></div><div class="rt-zone-section"><div class="rt-prompt-list" id="rt-custom-prompts"><div class="rt-prompt-empty">loading prompts...</div></div></div><div class="rt-start-agent-row"><button type="button" class="btn-primary rt-start-agent-btn" id="rt-start-agent" title="run the configured agent command in a new terminal.">start agent</button></div>'}
          </div>
        </div>`;
    const modalHtml = `
        ${isInspiration ? '<div class="rt-modal-backdrop" id="rt-create-goal-modal" hidden><div class="rt-modal-card"><div class="rt-modal-head"><strong>Create Goal</strong><button type="button" class="btn-ghost" id="rt-create-goal-modal-x">×</button></div><div class="rt-modal-body"><label for="rt-create-goal-select">Resource</label><select id="rt-create-goal-select">' + goalSelectOptionsHtml + '</select><div class="rt-goal-hint">Choose one resource file to generate a reviewable draft for Publish.</div></div><div class="rt-modal-actions"><button type="button" class="btn-ghost" id="rt-create-goal-cancel">Cancel</button><button type="button" class="btn-primary insp-create-btn" id="rt-create-goal-confirm">Create Goal</button></div></div></div>' : ''}
        ${taskDetailsModalHtml}
        ${startSettingsModalHtml}`;

    if(sideRootEl){
      rootEl.innerHTML = `<div class="rt-shell rt-shell-main-only">${terminalWrapHtml}${modalHtml}</div>`;
      sideRootEl.innerHTML = sideHtml;
    }else{
      rootEl.innerHTML = `
      <div class="rt-shell">
        ${terminalWrapHtml}
        ${sideHtml}
        ${modalHtml}
      </div>
    `;
    }

    const tabsEl = q('#rt-tabs');
    const bodyEl = q('#rt-body');
    const statusEl = q('#rt-status');
    const btnNew = q('#rt-new');
    const mouseSwitch = q('#rt-mouse-switch');
    const mouseText = q('#rt-mouse-text');
    const btnSendBasic = q('#rt-send-basic');
    const btnReportProgress = q('#rt-report-progress');
    const btnPua = q('#rt-pua');
    const customPromptsEl = q('#rt-custom-prompts');
    const btnStartAgent = q('#rt-start-agent');
    const btnStartAgentEdit = q('#rt-start-agent-edit');
    const btnDraftSummary = q('#rt-draft-summary');
    const btnCreateGoal = q('#rt-create-goal');
    const createGoalModal = q('#rt-create-goal-modal');
    const createGoalModalX = q('#rt-create-goal-modal-x');
    const createGoalCancel = q('#rt-create-goal-cancel');
    const createGoalConfirm = q('#rt-create-goal-confirm');
    const createGoalSelect = q('#rt-create-goal-select');
    const taskShow = q('#rt-task-show');
    const taskDetailsModal = q('#rt-task-details-modal');
    const taskDetailsX = q('#rt-task-details-x');
    const taskCleanup = q('#rt-task-cleanup');
    const taskElapsed = q('#rt-task-elapsed');
    const startSettingsModal = q('#rt-start-settings-modal');
    const startSettingsX = q('#rt-start-settings-x');
    const startSettingsCancel = q('#rt-start-settings-cancel');
    const startSettingsSave = q('#rt-start-settings-save');
    const startCommandInput = q('#rt-start-command-input');
    const filesFontInput = q('#rt-files-font-size');
    const previewFontInput = q('#rt-preview-font-size');
    const terminalFontInput = q('#rt-terminal-font-size');
    const showFilesInput = q('#rt-show-files');
    const showPreviewInput = q('#rt-show-preview');
    const showTerminalInput = q('#rt-show-terminal');
    const shortcutListEl = q('#rt-shortcut-list');
    const shortcutStatusEl = q('#rt-shortcut-status');

    const terminals = new Map(); // terminal_id -> { terminalId, name, tabEl, nameEl, viewEl, iframeEl }
    let activeId = '';
    let initialLoadPromise = null;
    let shortcutDraftSettings = loadAgentSpaceShortcuts();
    let recordingShortcutId = '';
    let recordingCapturedBinding = null;
    let recordingLastShiftAt = 0;

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
        const checked = p && p.auto_enabled ? ' checked' : '';
        return `<div class="rt-prompt-row"><button type="button" class="btn-ghost rt-prompt-btn rt-prompt-main" data-prompt-id="${id}" title="${title}: ${content}">${title}</button><label class="rt-auto-switch" title="append this prompt whenever a message is submitted"><input type="checkbox" data-auto-prompt-id="${id}"${checked} /><span>auto</span></label></div>`;
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
      syncAllAutoPrompts();
    }

    function customPromptText(prompt){
      const content = String(prompt && prompt.content ? prompt.content : '').trim();
      return content ? normalizeAutoPromptText(content) : '';
    }

    function applyBuiltinAutoUi(){
      if(isInspiration) return;
      qq('[data-auto-builtin]').forEach((el)=> {
        if(el instanceof HTMLInputElement) el.checked = loadBuiltinAutoPrompt(el.getAttribute('data-auto-builtin') || '');
      });
    }

    function autoPromptTexts(){
      if(isInspiration) return [];
      const out = [];
      if(loadBuiltinAutoPrompt('task_basic')) out.push(normalizeAutoPromptText(buildPasteText('task_basic')));
      if(loadBuiltinAutoPrompt('report_progress')) out.push(normalizeAutoPromptText(buildPasteText('report_progress')));
      if(loadBuiltinAutoPrompt('pua')) out.push(normalizeAutoPromptText(buildPasteText('pua')));
      for(const p of customPrompts){
        if(p && p.auto_enabled) out.push(customPromptText(p));
      }
      return out.map(normalizeAutoPromptText).filter(Boolean);
    }

    function combinedAutoPromptText(){
      return autoPromptTexts().join(' ');
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

    function applyAgentSpaceSettings(settings){
      const next = normalizeAgentSpaceSettings(settings || loadAgentSpaceSettings());
      rootEl.style.setProperty('--rt-terminal-font-size', `${next.terminalFontSize}px`);
      if(sideRootEl) sideRootEl.style.setProperty('--rt-terminal-font-size', `${next.terminalFontSize}px`);
      const terminalOpen = next.showTerminal ? '1' : '0';
      rootEl.dataset.terminalOpen = terminalOpen;
      if(sideRootEl) sideRootEl.dataset.terminalOpen = terminalOpen;
      qq('.rt-side').forEach((el)=> {
        if(el instanceof HTMLElement) el.dataset.terminalOpen = terminalOpen;
      });
      qq('[data-rt-pane]').forEach((btn)=> {
        if(!(btn instanceof HTMLElement)) return;
        const pane = String(btn.getAttribute('data-rt-pane') || '');
        const on = pane === 'files' ? next.showFiles : (pane === 'preview' ? next.showPreview : next.showTerminal);
        btn.classList.toggle('on', !!on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      rootEl.querySelectorAll('.rt-ttyd-frame').forEach((frame)=> {
        if(!(frame instanceof HTMLIFrameElement)) return;
        try{
          frame.contentWindow && frame.contentWindow.postMessage({
            type: 'openfocus:terminal-font-size',
            fontSize: next.terminalFontSize,
          }, window.location.origin);
        }catch(_){ }
      });
      return next;
    }

    function toggleAgentSpacePane(pane){
      const key = pane === 'files' ? 'showFiles' : (pane === 'preview' ? 'showPreview' : (pane === 'terminal' ? 'showTerminal' : ''));
      if(!key) return;
      const current = loadAgentSpaceSettings();
      const next = saveAgentSpaceSettings({ ...current, [key]: !current[key] }, 'terminal');
      applyAgentSpaceSettings(next);
    }

    function currentSettingsForInputs(){
      return normalizeAgentSpaceSettings({
        filesFontSize: filesFontInput && 'value' in filesFontInput ? filesFontInput.value : undefined,
        previewFontSize: previewFontInput && 'value' in previewFontInput ? previewFontInput.value : undefined,
        terminalFontSize: terminalFontInput && 'value' in terminalFontInput ? terminalFontInput.value : undefined,
        showFiles: showFilesInput instanceof HTMLInputElement ? showFilesInput.checked : true,
        showPreview: showPreviewInput instanceof HTMLInputElement ? showPreviewInput.checked : true,
        showTerminal: showTerminalInput instanceof HTMLInputElement ? showTerminalInput.checked : true,
      });
    }

    function currentShortcutSettingsForInputs(){
      return normalizeShortcutSettings(shortcutDraftSettings);
    }

    function setSettingsTab(name){
      const tabName = String(name || 'general');
      qq('[data-rt-settings-tab]').forEach((tab)=> {
        const on = tab.getAttribute('data-rt-settings-tab') === tabName;
        tab.classList.toggle('active', on);
        tab.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      qq('[data-rt-settings-panel]').forEach((panel)=> {
        panel.hidden = panel.getAttribute('data-rt-settings-panel') !== tabName;
      });
    }

    function shortcutStatus(text, isError){
      if(!shortcutStatusEl) return;
      shortcutStatusEl.textContent = String(text || '');
      shortcutStatusEl.classList.toggle('error', !!isError);
    }

    function renderShortcutSettings(){
      if(!shortcutListEl) return;
      const platform = currentShortcutPlatform();
      shortcutListEl.innerHTML = AGENT_SPACE_SHORTCUT_COMMANDS.map((cmd)=> {
        const current = resolveShortcutBinding(shortcutDraftSettings, cmd.id, platform);
        const def = resolveShortcutBinding(AGENT_SPACE_SHORTCUT_DEFAULTS, cmd.id, platform);
        const recording = recordingShortcutId === cmd.id;
        return `
          <div class="rt-shortcut-row" data-shortcut-command="${esc(cmd.id)}">
            <div class="rt-shortcut-main">
              <div class="rt-shortcut-label">${esc(cmd.label)}</div>
              <div class="rt-shortcut-scope">${esc(cmd.scope)}</div>
            </div>
            <div class="rt-shortcut-bindings">
              <div><span>Current</span><strong>${esc(recording ? 'Press shortcut' : formatShortcutBinding(current, platform))}</strong></div>
              <div><span>Default</span><strong>${esc(formatShortcutBinding(def, platform))}</strong></div>
            </div>
            <div class="rt-shortcut-actions">
              <button type="button" class="btn-ghost" data-shortcut-action="record" data-shortcut-command="${esc(cmd.id)}">${recording ? 'Recording' : 'Record'}</button>
              <button type="button" class="btn-ghost" data-shortcut-action="reset" data-shortcut-command="${esc(cmd.id)}">Reset</button>
              <button type="button" class="btn-ghost" data-shortcut-action="clear" data-shortcut-command="${esc(cmd.id)}">Clear</button>
            </div>
          </div>`;
      }).join('');
    }

    function replaceShortcutBinding(settings, commandId, binding, platform){
      const next = normalizeShortcutSettings(settings);
      const normalized = normalizeShortcutBinding({ ...binding, platform: binding.platform || platform || 'all' });
      if(!normalized) return next;
      const targetPlatform = platform || normalized.platform;
      next.bindings[commandId] = (next.bindings[commandId] || []).filter((existing)=> !shortcutPlatformsOverlap(existing.platform, targetPlatform));
      next.bindings[commandId].push(normalized);
      return next;
    }

    function clearShortcutBindingForPlatform(settings, commandId, platform){
      const next = normalizeShortcutSettings(settings);
      next.bindings[commandId] = (next.bindings[commandId] || []).filter((binding)=> !shortcutPlatformsOverlap(binding.platform, platform));
      return next;
    }

    function resetShortcutBindingForPlatform(settings, commandId, platform){
      const next = clearShortcutBindingForPlatform(settings, commandId, platform);
      const defaults = AGENT_SPACE_SHORTCUT_DEFAULTS.bindings[commandId] || [];
      next.bindings[commandId] = (next.bindings[commandId] || []).concat(defaults.filter((binding)=> shortcutPlatformMatches(binding.platform, platform)).map((binding)=> ({ keys: binding.keys.slice(), platform: binding.platform })));
      return next;
    }

    function startShortcutRecording(commandId){
      if(!AGENT_SPACE_SHORTCUT_COMMAND_BY_ID.has(commandId)) return;
      recordingShortcutId = commandId;
      recordingCapturedBinding = null;
      recordingLastShiftAt = 0;
      shortcutStatus('Press shortcut. Enter confirms, Backspace clears, Escape cancels.', false);
      renderShortcutSettings();
      try{ startSettingsModal && startSettingsModal.focus && startSettingsModal.focus(); }catch(_){ }
    }

    function cancelShortcutRecording(){
      recordingShortcutId = '';
      recordingCapturedBinding = null;
      recordingLastShiftAt = 0;
      shortcutStatus('', false);
      renderShortcutSettings();
    }

    function confirmShortcutRecording(){
      if(!recordingShortcutId || !recordingCapturedBinding) return;
      const platform = currentShortcutPlatform();
      const binding = { keys: recordingCapturedBinding.keys, platform };
      const validation = validateShortcutBinding(binding, shortcutDraftSettings, recordingShortcutId);
      if(!validation.ok){
        const message = validation.reason === 'conflict' && validation.conflict
          ? `Conflict with ${validation.conflict.label}. Clear or replace that shortcut first.`
          : (validation.reason === 'reserved_browser_shortcut' ? 'Browser-reserved shortcut.' : 'Shortcut needs a modifier, function key, or Double Shift.');
        shortcutStatus(message, true);
        return;
      }
      shortcutDraftSettings = replaceShortcutBinding(shortcutDraftSettings, recordingShortcutId, validation.binding, platform);
      recordingShortcutId = '';
      recordingCapturedBinding = null;
      recordingLastShiftAt = 0;
      shortcutStatus('Shortcut updated. Save to persist.', false);
      renderShortcutSettings();
    }

    function captureShortcutRecording(event){
      if(!recordingShortcutId) return false;
      if(event && event.preventDefault) event.preventDefault();
      if(event && event.stopPropagation) event.stopPropagation();
      const key = normalizeShortcutKeyName(event && event.key);
      if(key === 'Escape'){
        cancelShortcutRecording();
        return true;
      }
      if(key === 'Backspace'){
        shortcutDraftSettings = clearShortcutBindingForPlatform(shortcutDraftSettings, recordingShortcutId, currentShortcutPlatform());
        cancelShortcutRecording();
        shortcutStatus('Shortcut cleared. Save to persist.', false);
        return true;
      }
      if(key === 'Enter'){
        confirmShortcutRecording();
        return true;
      }
      if(key === 'Shift'){
        const now = Date.now();
        if(now - recordingLastShiftAt <= 650){
          recordingCapturedBinding = { keys: ['Shift', 'Shift'], platform: 'all' };
          shortcutStatus('Double Shift captured. Press Enter to confirm.', false);
          renderShortcutSettings();
        }else{
          shortcutStatus('Press Shift again for Double Shift.', false);
        }
        recordingLastShiftAt = now;
        return true;
      }
      const binding = normalizeShortcutKeyEvent(event);
      if(!binding) return true;
      recordingCapturedBinding = binding;
      const validation = validateShortcutBinding({ keys: binding.keys, platform: currentShortcutPlatform() }, shortcutDraftSettings, recordingShortcutId);
      if(!validation.ok){
        const message = validation.reason === 'conflict' && validation.conflict
          ? `Conflict with ${validation.conflict.label}.`
          : (validation.reason === 'reserved_browser_shortcut' ? 'Browser-reserved shortcut.' : 'Shortcut needs a modifier, function key, or Double Shift.');
        shortcutStatus(message, true);
      }else{
        shortcutStatus(`${formatShortcutBinding(binding, currentShortcutPlatform())} captured. Press Enter to confirm.`, false);
      }
      renderShortcutSettings();
      return true;
    }

    function fillStartSettingsModal(){
      const settings = loadAgentSpaceSettings();
      shortcutDraftSettings = loadAgentSpaceShortcuts();
      recordingShortcutId = '';
      recordingCapturedBinding = null;
      recordingLastShiftAt = 0;
      if(startCommandInput && 'value' in startCommandInput) startCommandInput.value = String(startAgentCommand || '');
      if(filesFontInput && 'value' in filesFontInput) filesFontInput.value = String(settings.filesFontSize);
      if(previewFontInput && 'value' in previewFontInput) previewFontInput.value = String(settings.previewFontSize);
      if(terminalFontInput && 'value' in terminalFontInput) terminalFontInput.value = String(settings.terminalFontSize);
      if(showFilesInput instanceof HTMLInputElement) showFilesInput.checked = !!settings.showFiles;
      if(showPreviewInput instanceof HTMLInputElement) showPreviewInput.checked = !!settings.showPreview;
      if(showTerminalInput instanceof HTMLInputElement) showTerminalInput.checked = !!settings.showTerminal;
      shortcutStatus('', false);
      renderShortcutSettings();
    }

    let startSettingsResolve = null;

    function closeStartSettingsModal(value){
      if(startSettingsModal) startSettingsModal.hidden = true;
      const resolve = startSettingsResolve;
      startSettingsResolve = null;
      if(resolve) resolve(String(value || ''));
      focusActive();
    }

    function openStartSettingsModal(){
      if(!startSettingsModal) return Promise.resolve('');
      fillStartSettingsModal();
      setSettingsTab('general');
      startSettingsModal.hidden = false;
      try{ startCommandInput && startCommandInput.focus && startCommandInput.focus(); }catch(_){ }
      return new Promise((resolve)=> {
        startSettingsResolve = resolve;
      });
    }

    async function saveStartSettingsModal(){
      const command = String(startCommandInput && 'value' in startCommandInput ? startCommandInput.value : '').trim();
      const savedCommand = await saveStartAgentCommand(command);
      const settings = saveAgentSpaceSettings(currentSettingsForInputs(), 'terminal');
      saveAgentSpaceShortcuts(currentShortcutSettingsForInputs(), 'terminal');
      applyAgentSpaceSettings(settings);
      toast(savedCommand ? 'AgentSpace settings saved' : 'Start Agent command cleared');
      closeStartSettingsModal(savedCommand);
      return savedCommand;
    }

    async function editStartAgentCommand(){
      return openStartSettingsModal();
    }

    function pasteToActive(text){
      const s = String(text || '');
      if(!s) return;
      void injectPromptToTerminal(activeTerminal(), s, { bracketedPaste: true, focus: true }).then((ok)=>{
        if(!ok) toast('terminal unavailable');
      }).catch((err)=>{
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

    function openTaskDetailsModal(){
      if(!taskDetailsModal) return;
      if(taskElapsed) taskElapsed.textContent = formatElapsedFrom(spaceCreatedAt);
      taskDetailsModal.hidden = false;
    }

    function closeTaskDetailsModal(){
      if(taskDetailsModal) taskDetailsModal.hidden = true;
      focusActive();
    }

    async function cleanupTaskAgentSpace(){
      if(!taskPublicId) return;
      if(!confirm('Release this AgentSpace? This deletes OpenFocus records and terminal records, but does not delete local files.')) return;
      try{
        await fetchJson(`/api/tasks/${encodeURIComponent(taskPublicId)}/agent_space`, { method: 'DELETE' });
        toast('Released');
        window.location.href = taskUrl || `/goals?task=${encodeURIComponent(taskPublicId)}`;
      }catch(err){
        toast('Release failed');
        alert('Release failed: ' + String(err && err.message ? err.message : err));
      }
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

    function syncTtydAutoPrompts(it){
      if(isInspiration) return;
      if(!it || !it.iframeEl) return;
      const prompt = combinedAutoPromptText();
      try{
        fetchJson(`${apiBase}/${encodeURIComponent(it.terminalId)}/auto_prompts`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: !!prompt, prompt }),
        }).catch(()=>{});
      }catch(_){ }
    }

    function syncAllAutoPrompts(){
      if(isInspiration) return;
      for(const it of terminals.values()) syncTtydAutoPrompts(it);
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

    function attachTtydAutoPromptSync(it){
      if(isInspiration) return;
      if(!it || !it.iframeEl) return;
      try{ it.iframeEl.addEventListener('load', ()=> syncTtydAutoPrompts(it)); }catch(_){ }
      setTimeout(()=> syncTtydAutoPrompts(it), 300);
      setTimeout(()=> syncTtydAutoPrompts(it), 1200);
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
        applyMouseUi();
        syncTtydAutoPrompts(it);
      }
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
      await injectPromptToTerminal(it, cmd, { bracketedPaste: false, submit: true, focus: true });
      toast('Agent started');
    }

    function clearAutoStartLocationFlag(){
      try{
        const url = new URL(window.location.href);
        if(!url.searchParams.has('autostart')) return;
        url.searchParams.delete('autostart');
        window.history.replaceState(window.history.state, '', url.pathname + (url.search || '') + (url.hash || ''));
      }catch(_){ }
    }

    async function maybeAutoStartDefaultTerminal(it){
      if(isInspiration || !autoStartDefaultTerminalPending) return;
      autoStartDefaultTerminalPending = false;
      clearAutoStartLocationFlag();
      const cmd = String(startAgentCommand || '').trim();
      if(!cmd) return;
      if(!it){ toast('terminal unavailable'); return; }
      try{
        const ok = await injectPromptToTerminal(it, cmd, { bracketedPaste: false, submit: true, focus: true });
        toast(ok ? 'Agent started' : 'terminal unavailable');
      }catch(err){
        toast(String(err && err.message ? err.message : err || 'start failed'));
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
      applyAgentSpaceSettings();

      const it = { terminalId: tid, name: nm, backend: 'ttyd', embedUrl, iframeEl, tabEl: tab, nameEl, viewEl: view };
      it.__mouse_mode = loadMouseMode(tid);
      terminals.set(tid, it);
      try{ iframeEl.addEventListener('load', ()=> applyAgentSpaceSettings()); }catch(_){ }
      attachTtydAutoPromptSync(it);
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
        const it = await createNew();
        await maybeAutoStartDefaultTerminal(it);
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
    btnSendBasic?.addEventListener('click', ()=> {
      const text = buildPasteText('task_basic');
      if(!String(text || '').trim()){
        toast('Task Basic is empty');
        return;
      }
      pasteToActive(text);
    });
    btnReportProgress?.addEventListener('click', ()=> pasteToActive(buildPasteText('report_progress')));
    btnPua?.addEventListener('click', ()=> pasteToActive(buildPasteText('pua')));
    qq('[data-auto-builtin]').forEach((el)=> {
      el.addEventListener('change', ()=>{
        if(!(el instanceof HTMLInputElement)) return;
        const key = String(el.getAttribute('data-auto-builtin') || '').trim();
        saveBuiltinAutoPrompt(key, !!el.checked);
        syncAllAutoPrompts();
        toast(el.checked ? 'Auto prompt: on' : 'Auto prompt: off');
        focusActive();
      });
    });
    qq('[data-rt-pane]').forEach((el)=> {
      el.addEventListener('click', ()=>{
        if(!(el instanceof HTMLElement)) return;
        toggleAgentSpacePane(String(el.getAttribute('data-rt-pane') || ''));
      });
    });
    customPromptsEl?.addEventListener('click', (e)=> {
      const target = e && e.target && e.target.closest ? e.target.closest('[data-prompt-id]') : null;
      if(!target) return;
      const id = Number(target.getAttribute('data-prompt-id') || 0);
      const prompt = customPrompts.find((p)=> Number(p && p.id ? p.id : 0) === id);
      pasteToActive(customPromptText(prompt));
    });
    customPromptsEl?.addEventListener('change', (e)=> {
      const target = e && e.target && e.target.closest ? e.target.closest('[data-auto-prompt-id]') : null;
      if(!target || !(target instanceof HTMLInputElement)) return;
      const id = Number(target.getAttribute('data-auto-prompt-id') || 0);
      const prompt = customPrompts.find((p)=> Number(p && p.id ? p.id : 0) === id);
      if(!prompt) return;
      const previous = !target.checked;
      const next = !!target.checked;
      prompt.auto_enabled = next;
      syncAllAutoPrompts();
      fetchJson(`${promptApi}/${encodeURIComponent(String(id))}/auto_enabled`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto_enabled: next }),
      }).then((data)=> {
        if(data && data.item){
          const idx = customPrompts.findIndex((p)=> Number(p && p.id ? p.id : 0) === id);
          if(idx >= 0) customPrompts[idx] = data.item;
          renderCustomPrompts();
          syncAllAutoPrompts();
        }
        toast(next ? 'Auto prompt: on' : 'Auto prompt: off');
      }).catch((err)=> {
        prompt.auto_enabled = previous;
        target.checked = previous;
        syncAllAutoPrompts();
        toast(String(err && err.message ? err.message : err || 'auto prompt failed'));
      }).finally(()=> focusActive());
    });
    btnStartAgent?.addEventListener('click', ()=> {
      void startAgent().catch((err)=> toast(String(err && err.message ? err.message : err || 'start failed')));
    });
    btnStartAgentEdit?.addEventListener('click', ()=> {
      void editStartAgentCommand().catch((err)=> toast(String(err && err.message ? err.message : err || 'save failed')));
    });
    taskShow?.addEventListener('click', openTaskDetailsModal);
    taskDetailsX?.addEventListener('click', closeTaskDetailsModal);
    taskCleanup?.addEventListener('click', ()=> {
      void cleanupTaskAgentSpace();
    });
    taskDetailsModal?.addEventListener('click', (e)=>{ if(e.target === taskDetailsModal) closeTaskDetailsModal(); });
    qq('[data-rt-settings-tab]').forEach((tab)=> {
      tab.addEventListener('click', ()=> setSettingsTab(tab.getAttribute('data-rt-settings-tab') || 'general'));
    });
    shortcutListEl?.addEventListener('click', (e)=> {
      const target = e.target && e.target.closest ? e.target.closest('[data-shortcut-action]') : null;
      if(!target) return;
      const commandId = String(target.getAttribute('data-shortcut-command') || '');
      const action = String(target.getAttribute('data-shortcut-action') || '');
      if(!AGENT_SPACE_SHORTCUT_COMMAND_BY_ID.has(commandId)) return;
      if(action === 'record'){
        startShortcutRecording(commandId);
      }else if(action === 'reset'){
        shortcutDraftSettings = resetShortcutBindingForPlatform(shortcutDraftSettings, commandId, currentShortcutPlatform());
        recordingShortcutId = '';
        recordingCapturedBinding = null;
        shortcutStatus('Shortcut reset. Save to persist.', false);
        renderShortcutSettings();
      }else if(action === 'clear'){
        shortcutDraftSettings = clearShortcutBindingForPlatform(shortcutDraftSettings, commandId, currentShortcutPlatform());
        recordingShortcutId = '';
        recordingCapturedBinding = null;
        shortcutStatus('Shortcut cleared. Save to persist.', false);
        renderShortcutSettings();
      }
    });
    startSettingsX?.addEventListener('click', ()=> closeStartSettingsModal(''));
    startSettingsCancel?.addEventListener('click', ()=> closeStartSettingsModal(''));
    startSettingsSave?.addEventListener('click', ()=> {
      void saveStartSettingsModal().catch((err)=> toast(String(err && err.message ? err.message : err || 'save failed')));
    });
    startSettingsModal?.addEventListener('click', (e)=>{ if(e.target === startSettingsModal) closeStartSettingsModal(''); });
    startSettingsModal?.addEventListener('keydown', (e)=>{
      if(recordingShortcutId && captureShortcutRecording(e)) return;
      if(e && e.key === 'Escape') closeStartSettingsModal('');
      if(e && e.key === 'Enter' && (e.metaKey || e.ctrlKey)){
        e.preventDefault();
        void saveStartSettingsModal().catch((err)=> toast(String(err && err.message ? err.message : err || 'save failed')));
      }
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
    window.addEventListener(AGENT_SPACE_SETTINGS_EVENT, (event)=> {
      const detail = event && event.detail && typeof event.detail === 'object' ? event.detail : {};
      applyAgentSpaceSettings(detail.settings || loadAgentSpaceSettings());
    });
    window.addEventListener('storage', (event)=> {
      if(event && event.key === AGENT_SPACE_SETTINGS_KEY) applyAgentSpaceSettings();
    });
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
      applyAgentSpaceSettings,
    };
    try{ rootEl.__openfocusRemoteTerminal = api; }catch(_){ }
    try{ if(sideRootEl) sideRootEl.__openfocusRemoteTerminal = api; }catch(_){ }

    initialLoadPromise = loadExisting();
    applyBuiltinAutoUi();
    applyMouseUi();
    applyStartAgentUi();
    applyAgentSpaceSettings();
    void loadCustomPrompts();
    return api;
  }

  window.OpenFocusRemoteTerminal = { mount };
})();

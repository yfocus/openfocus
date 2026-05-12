/* SPDX-License-Identifier: Apache-2.0 */
import { createRoot } from 'react-dom/client';
import React, { useCallback, useEffect, useRef } from 'react';
import { apiJson } from '../api/client';
import {
  closeSpace,
  createInspiration,
  createResource,
  deleteResource,
  deleteSpace,
  forkSpace,
  generateDraftFromResource,
  getInspiration,
  postMessage,
  publishDraft,
  reopenSpace,
  replaceResource,
  syncResources,
  syncResourcesUrl,
  terminalApiBase,
  updateResource,
} from '../api/inspirations';

type InspirationConfig = {
  hasSpace: boolean;
  spaceId: number;
  isTerminalMode: boolean;
  isWaiting: boolean;
  isPublishing: boolean;
  forkTitle: string;
  draftSummaryPrompt: string;
  resourceIds: number[];
};

function toast(message: string): void {
  if (typeof window.toast === 'function') window.toast(message);
}

const callJson = apiJson;

function InspirationSpaceController({ config }: { config: InspirationConfig }) {
  const stateRef = useRef({
    hasSpace: config.hasSpace,
    waiting: config.isWaiting,
    publishing: config.isPublishing,
    resourceModalMode: 'create',
    resourceModalResourceId: 0,
    busyPollTimer: 0,
  });

  const getRoot = useCallback(() => document.getElementById('insp-page-root'), []);

  const syncStateFromRoot = useCallback(() => {
    const root = getRoot();
    if (!root) return;
    stateRef.current.hasSpace = root.dataset.hasSpace === 'true';
    stateRef.current.waiting = root.dataset.isWaiting === 'true';
    stateRef.current.publishing = root.dataset.isPublishing === 'true';
  }, [getRoot]);

  const scrollChat = useCallback(() => {
    const chat = document.getElementById('insp-chat');
    if (chat) chat.scrollTop = chat.scrollHeight;
  }, []);

  const captureScrollState = () => ({
    windowY: window.scrollY || 0,
    chatY: document.getElementById('insp-chat')?.scrollTop || 0,
    resourcesY: document.getElementById('insp-resources-scroll')?.scrollTop || 0,
  });

  const restoreScrollState = (state: { windowY: number; chatY: number; resourcesY: number }) => {
    window.scrollTo(0, Number(state.windowY || 0));
    const chat = document.getElementById('insp-chat');
    if (chat) chat.scrollTop = Number(state.chatY || 0);
    const resources = document.getElementById('insp-resources-scroll');
    if (resources) resources.scrollTop = Number(state.resourcesY || 0);
  };

  const setPublishingUI = useCallback((on: boolean, activeButton?: HTMLButtonElement | null) => {
    stateRef.current.publishing = !!on;
    const { publishing, waiting, hasSpace } = stateRef.current;
    const mask = document.getElementById('insp-publish-mask') as HTMLElement | null;
    const publishState = document.getElementById('insp-publish-state') as HTMLElement | null;
    const terminalMask = document.getElementById('insp-terminal-input-mask') as HTMLElement | null;
    if (mask) mask.hidden = !publishing;
    if (publishState) publishState.style.display = publishing ? 'inline' : 'none';
    if (terminalMask) {
      terminalMask.hidden = !(publishing || waiting);
      terminalMask.innerHTML = `<span class="spin"></span> ${publishing ? 'Publishing…' : 'Create Goal ongoing…'}`;
    }
    document.querySelectorAll<HTMLButtonElement | HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>('.insp-work-area button, .insp-work-area input, .insp-work-area textarea, .insp-work-area select').forEach((el) => {
      if (activeButton && el === activeButton) {
        el.disabled = publishing;
        el.textContent = publishing ? 'Publishing...' : String(el.dataset.defaultLabel || 'Publish');
        return;
      }
      el.disabled = publishing || (waiting && el.id !== 'insp-open-create-modal');
      if ('readOnly' in el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) el.readOnly = publishing || waiting;
    });
    document.querySelectorAll<HTMLButtonElement>('[data-resource-send]').forEach((el) => { el.disabled = publishing || waiting || !hasSpace; });
  }, []);

  const setWaitingUI = useCallback((on: boolean) => {
    stateRef.current.waiting = !!on;
    const { waiting, publishing, hasSpace } = stateRef.current;
    const hint = document.getElementById('insp-message-hint') as HTMLElement | null;
    const terminalMask = document.getElementById('insp-terminal-input-mask') as HTMLElement | null;
    if (terminalMask) {
      terminalMask.hidden = !(waiting || publishing);
      terminalMask.innerHTML = `<span class="spin"></span> ${publishing ? 'Publishing…' : 'Create Goal ongoing…'}`;
    }
    if (hint) {
      hint.style.visibility = waiting || publishing ? 'visible' : 'hidden';
      hint.innerHTML = `<span class="spin"></span> ${publishing ? 'Publishing...' : waiting ? 'Waiting for agent…' : 'Sending…'}`;
    }
    if (!publishing) {
      document.querySelectorAll<HTMLButtonElement | HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>('.insp-work-area button, .insp-work-area input, .insp-work-area textarea, .insp-work-area select').forEach((el) => {
        el.disabled = waiting;
        if ('readOnly' in el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) el.readOnly = waiting;
      });
    }
    document.querySelectorAll<HTMLButtonElement>('[data-resource-send]').forEach((el) => { el.disabled = waiting || publishing || !hasSpace; });
  }, []);

  const terminalApi = () => document.getElementById('insp-remote-terminal')?.__openfocusRemoteTerminal || null;

  const bindUIRef = useRef<() => void>(() => undefined);
  const scheduleBusyPollRef = useRef<(delay?: number) => void>(() => undefined);

  const refreshPageFromServer = useCallback(async (options?: { scrollChat?: boolean }) => {
    const prevScroll = captureScrollState();
    const response = await fetch(window.location.pathname, { headers: { 'X-Requested-With': 'fetch' } });
    if (!response.ok) throw new Error(await response.text().catch(() => 'refresh failed'));
    const html = await response.text();
    const nextDoc = new DOMParser().parseFromString(html, 'text/html');
    const nextRoot = nextDoc.getElementById('insp-page-root');
    const currentRoot = getRoot();
    if (!nextRoot || !currentRoot) throw new Error('missing inspiration root');
    currentRoot.replaceWith(nextRoot);
    syncStateFromRoot();
    bindUIRef.current();
    restoreScrollState(prevScroll);
    if (options?.scrollChat) scrollChat();
  }, [getRoot, scrollChat, syncStateFromRoot]);

  const scheduleBusyPoll = useCallback((delay = 900) => {
    const state = stateRef.current;
    if (state.busyPollTimer) window.clearTimeout(state.busyPollTimer);
    if (!state.hasSpace || !config.spaceId || (!state.waiting && !state.publishing)) return;
    state.busyPollTimer = window.setTimeout(async () => {
      try {
        const data = await getInspiration(config.spaceId);
        const nextWaiting = !!data.is_waiting;
        const nextPublishing = !!data.is_publishing;
        if (nextWaiting || nextPublishing) {
          stateRef.current.waiting = nextWaiting;
          stateRef.current.publishing = nextPublishing;
          setWaitingUI(nextWaiting);
          setPublishingUI(nextPublishing);
          scheduleBusyPollRef.current(900);
          return;
        }
        await refreshPageFromServer({ scrollChat: true });
      } catch (_) {
        scheduleBusyPollRef.current(1400);
      }
    }, Number(delay || 900));
  }, [config.spaceId, refreshPageFromServer, setPublishingUI, setWaitingUI]);

  scheduleBusyPollRef.current = scheduleBusyPoll;

  const appendResourceReference = (reference: string, card: HTMLElement | null) => {
    const { waiting, publishing, hasSpace } = stateRef.current;
    if (waiting || publishing || !hasSpace) return;
    if (config.isTerminalMode) {
      const api = terminalApi();
      if (!api?.injectPromptToTerminal) {
        toast('Terminal is not ready');
        return;
      }
      const externalPath = String(card?.dataset.resourcePath || '').trim();
      const text = externalPath ? (externalPath.startsWith('./') || externalPath.startsWith('/') ? externalPath : `./${externalPath}`) : String(reference || '');
      if (!text.trim()) { toast('Nothing to send'); return; }
      void api.injectPromptToTerminal(text, { bracketedPaste: true, submit: false, focus: true })
        .then((ok) => { if (ok) toast('Sent to terminal'); })
        .catch((err: unknown) => toast(err instanceof Error ? err.message : String(err || 'send failed')));
      return;
    }
    const input = document.getElementById('insp-message-input') as HTMLTextAreaElement | null;
    if (!input || input.disabled || input.readOnly) return;
    const current = String(input.value || '');
    input.value = current ? (current.endsWith(' ') || current.endsWith('\n') ? current + reference : `${current} ${reference}`) : reference;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  };

  const appendPendingTurn = (userText: string, pendingText: string) => {
    const chat = document.getElementById('insp-chat');
    if (!chat) return;
    const userRow = document.createElement('div');
    userRow.className = 'msg-row user';
    userRow.innerHTML = '<div class="msg user"><div class="pad"><div class="msg-role">you</div><div class="msg-text"></div></div></div>';
    const text = userRow.querySelector('.msg-text');
    if (text) text.textContent = userText;
    chat.appendChild(userRow);
    const agentRow = document.createElement('div');
    agentRow.className = 'msg-row agent';
    agentRow.innerHTML = `<div class="msg pending"><div class="pad"><div class="msg-role">pending</div><div class="msg-text"><span class="spin"></span> ${pendingText}</div></div></div>`;
    chat.appendChild(agentRow);
    scrollChat();
  };

  const setCreateModal = (open: boolean) => {
    const modal = document.getElementById('insp-create-modal') as HTMLElement | null;
    if (modal) modal.hidden = !open;
  };

  const setResourceModal = (open: boolean) => {
    const modal = document.getElementById('insp-resource-modal') as HTMLElement | null;
    if (modal) modal.hidden = !open;
  };

  const resourceCardFromNode = (node: Element | null): HTMLElement | null => node?.closest<HTMLElement>('[data-resource-card]') || null;
  const resourceCardValue = (card: HTMLElement | null, selector: string): string => {
    const el = card?.querySelector(selector) as HTMLInputElement | HTMLTextAreaElement | HTMLElement | null;
    if (!el) return '';
    return 'value' in el ? String(el.value || '') : String(el.textContent || '');
  };

  const syncResourceForm = () => {
    const mode = stateRef.current.resourceModalMode;
    const type = String((document.getElementById('insp-resource-type') as HTMLSelectElement | null)?.value || 'text');
    const textWrap = document.getElementById('insp-resource-text-wrap') as HTMLElement | null;
    const urlWrap = document.getElementById('insp-resource-url-wrap') as HTMLElement | null;
    const fileWrap = document.getElementById('insp-resource-file-wrap') as HTMLElement | null;
    if (mode === 'replace') {
      if (textWrap) textWrap.style.display = 'none';
      if (urlWrap) urlWrap.style.display = 'none';
      if (fileWrap) fileWrap.style.display = 'block';
      return;
    }
    if (textWrap) textWrap.style.display = type === 'text' ? 'block' : 'none';
    if (urlWrap) urlWrap.style.display = type === 'url' ? 'block' : 'none';
    if (fileWrap) fileWrap.style.display = mode === 'create' && type === 'image' ? 'block' : 'none';
  };

  const configureResourceModal = (options: { mode?: string; type?: string; resourceId?: number; name?: string; text?: string; url?: string }) => {
    const mode = String(options.mode || 'create');
    const resourceType = String(options.type || 'text');
    stateRef.current.resourceModalMode = mode;
    stateRef.current.resourceModalResourceId = Number(options.resourceId || 0);
    const form = document.getElementById('insp-resource-form') as HTMLFormElement | null;
    form?.reset();
    const title = document.getElementById('insp-resource-modal-title');
    const hint = document.getElementById('insp-resource-hint');
    const btn = document.getElementById('insp-resource-btn');
    const resourceIdInput = document.getElementById('insp-resource-id') as HTMLInputElement | null;
    const typeWrap = document.getElementById('insp-resource-type-wrap') as HTMLElement | null;
    const typeInput = document.getElementById('insp-resource-type') as HTMLSelectElement | null;
    const nameInput = document.getElementById('insp-resource-name') as HTMLInputElement | null;
    const textInput = document.getElementById('insp-resource-text') as HTMLTextAreaElement | null;
    const urlInput = document.getElementById('insp-resource-url') as HTMLInputElement | null;
    const fileInput = document.getElementById('insp-resource-file') as HTMLInputElement | null;
    if (resourceIdInput) resourceIdInput.value = stateRef.current.resourceModalResourceId ? String(stateRef.current.resourceModalResourceId) : '';
    if (typeWrap) typeWrap.style.display = mode === 'create' ? 'block' : 'none';
    if (typeInput) {
      typeInput.value = resourceType;
      typeInput.disabled = mode !== 'create' || stateRef.current.waiting || stateRef.current.publishing;
    }
    if (nameInput) nameInput.value = String(options.name || '');
    if (textInput) textInput.value = String(options.text || '');
    if (urlInput) urlInput.value = String(options.url || '');
    if (fileInput) fileInput.value = '';
    if (title) title.textContent = mode === 'edit' ? 'Edit Resource' : mode === 'replace' ? 'Replace Image' : 'Add Resource';
    if (btn) btn.textContent = mode === 'edit' ? 'Save' : mode === 'replace' ? 'Replace' : 'Add Resource';
    if (hint) hint.innerHTML = `<span class="spin"></span> ${mode === 'edit' ? 'Saving…' : mode === 'replace' ? 'Replacing…' : 'Uploading…'}`;
    syncResourceForm();
  };

  const syncToggleButton = (btn: HTMLElement | null) => {
    const preview = btn?.closest('.pad')?.querySelector<HTMLElement>('[data-resource-preview]');
    if (!btn || !preview) return;
    const threshold = Number(btn.dataset.collapseThreshold || preview.dataset.collapseThreshold || (preview.hasAttribute('data-media-preview') ? 168 : 64));
    const isMedia = preview.hasAttribute('data-media-preview');
    const collapsible = isMedia ? preview.scrollHeight > threshold + 2 : (preview.scrollHeight > threshold + 2 || preview.scrollWidth > preview.clientWidth + 2);
    if (preview.dataset.autoCollapse === 'true' && preview.dataset.collapseInitialized !== 'true') {
      if (collapsible) preview.classList.add('collapsed');
      preview.dataset.collapseInitialized = 'true';
    }
    btn.hidden = !collapsible;
    if (!collapsible) preview.classList.remove('collapsed');
    btn.textContent = preview.classList.contains('collapsed') ? 'Expand' : 'Collapse';
  };

  const copyResourceContent = async (card: HTMLElement | null) => {
    if (!card) return;
    const resourceType = String(card.dataset.resourceType || 'text');
    const text = resourceType === 'url' ? resourceCardValue(card, '[data-resource-url]') : resourceCardValue(card, '[data-resource-text]');
    if (!text.trim()) { toast('Nothing to copy'); return; }
    try {
      await navigator.clipboard.writeText(text);
      toast('Copied');
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err || 'copy failed'));
    }
  };

  const submitTurn = async (content: string) => {
    const text = String(content || '').trim();
    if (!text || !stateRef.current.hasSpace) return;
    const pendingText = text === '/summary_title' ? 'Generating title suggestions…' : text === '/plan' ? 'Generating a publish-ready draft…' : 'Thinking…';
    appendPendingTurn(text, pendingText);
    setWaitingUI(true);
    try {
      await postMessage(config.spaceId, text);
      scheduleBusyPoll(400);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err || 'send failed'));
      try { await refreshPageFromServer({ scrollChat: true }); } catch (_) { setWaitingUI(false); }
    }
  };

  const mountTerminal = () => {
    const termRoot = document.getElementById('insp-remote-terminal');
    if (!termRoot || !window.OpenFocusRemoteTerminal?.mount) return;
    const goalResources = config.resourceIds.map((id) => {
      const card = document.querySelector<HTMLElement>(`[data-resource-card][data-resource-id="${String(id)}"]`);
      const title = card ? String(card.querySelector('.card-title')?.textContent || '').trim() : `Resource #${id}`;
      const seq = Number((title.match(/^#(\d+)/) || [])[1] || 0);
      return { id, seq, title };
    });
    const resolveGoalResource = (selection: string) => {
      if (!goalResources.length) return null;
      const raw = String(selection || '').trim();
      if (!raw) return goalResources.length === 1 ? goalResources[0] : null;
      const numeric = Number(raw.replace(/^#/, ''));
      if (Number.isFinite(numeric) && numeric > 0) {
        const byNumber = goalResources.find((r, idx) => Number(r.id) === numeric || Number(r.seq) === numeric || idx + 1 === numeric);
        if (byNumber) return byNumber;
      }
      const lower = raw.toLowerCase();
      return goalResources.find((r) => r.title.toLowerCase() === lower) || goalResources.find((r) => r.title.toLowerCase().includes(lower)) || null;
    };
    window.OpenFocusRemoteTerminal.mount(termRoot, {
      spaceId: config.spaceId,
      mode: 'inspiration',
      apiBase: terminalApiBase(config.spaceId),
      syncUrl: syncResourcesUrl(config.spaceId),
      draftSummaryPrompt: config.draftSummaryPrompt,
      agentPrefix: `OpenFocus Inspiration #${config.spaceId} · write resources/draft_summary.md; do not create Goals/Tasks directly`,
      goalResources,
      createGoalFromResource: async (selection: string) => {
        if (stateRef.current.waiting || stateRef.current.publishing) return;
        if (!goalResources.length) { toast('No resources to use'); return; }
        const item = resolveGoalResource(selection);
        if (!item) { toast('Type or choose a resource first'); return; }
        setWaitingUI(true);
        try {
          await generateDraftFromResource(config.spaceId, item.id);
          scheduleBusyPoll(400);
        } catch (err) {
          setWaitingUI(false);
          toast(err instanceof Error ? err.message : String(err || 'create failed'));
        }
      },
    });
  };

  const bindUI = useCallback(() => {
    const createModal = document.getElementById('insp-create-modal');
    const resourceModal = document.getElementById('insp-resource-modal');
    document.getElementById('insp-open-create-modal')?.addEventListener('click', () => setCreateModal(true));
    document.getElementById('insp-create-cancel')?.addEventListener('click', () => setCreateModal(false));
    createModal?.addEventListener('click', (event) => { if (event.target === createModal) setCreateModal(false); });
    document.getElementById('insp-open-resource-modal')?.addEventListener('click', () => {
      if (stateRef.current.waiting || stateRef.current.publishing) return;
      configureResourceModal({ mode: 'create', type: 'text' });
      setResourceModal(true);
    });
    document.getElementById('insp-resource-cancel')?.addEventListener('click', () => setResourceModal(false));
    resourceModal?.addEventListener('click', (event) => { if (event.target === resourceModal) setResourceModal(false); });

    document.getElementById('create-inspiration-form')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const title = String((document.getElementById('insp-title') as HTMLTextAreaElement | null)?.value || '').trim();
      const initialMessage = String((document.getElementById('insp-message') as HTMLTextAreaElement | null)?.value || '').trim();
      const mode = String((document.getElementById('insp-mode') as HTMLSelectElement | null)?.value || 'built_in');
      if (!title && !initialMessage) { toast('Enter a Title or First Note'); return; }
      const btn = document.getElementById('insp-create-btn') as HTMLButtonElement | null;
      const hint = document.getElementById('insp-create-hint') as HTMLElement | null;
      if (btn) btn.disabled = true;
      if (hint) hint.style.display = 'inline-flex';
      try {
        const data = await createInspiration({ title, initial_message: initialMessage, mode });
        const id = data.item?.id || 0;
        if (!id) throw new Error('missing space id');
        window.location.href = `/inspirations/${id}`;
      } catch (err) {
        toast(err instanceof Error ? err.message : String(err || 'create failed'));
        if (btn) btn.disabled = false;
        if (hint) hint.style.display = 'none';
      }
    });

    document.getElementById('insp-resource-type')?.addEventListener('change', syncResourceForm);
    syncResourceForm();

    document.getElementById('insp-resource-form')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const { waiting, publishing, hasSpace, resourceModalMode, resourceModalResourceId } = stateRef.current;
      if (waiting || publishing || !hasSpace) return;
      const form = event.currentTarget as HTMLFormElement;
      const fd = new FormData(form);
      const nextType = String((document.getElementById('insp-resource-type') as HTMLSelectElement | null)?.value || 'text');
      const hint = document.getElementById('insp-resource-hint') as HTMLElement | null;
      const btn = document.getElementById('insp-resource-btn') as HTMLButtonElement | null;
      if (hint) hint.style.display = 'inline-flex';
      if (btn) btn.disabled = true;
      try {
        if (resourceModalMode === 'edit') {
          const payload: { name: string; url_content?: string; text_content?: string } = { name: String(fd.get('name') || '').trim() };
          if (nextType === 'url') payload.url_content = String(fd.get('url_content') || '');
          else payload.text_content = String(fd.get('text_content') || '');
          await updateResource(config.spaceId, resourceModalResourceId, payload);
        } else if (resourceModalMode === 'replace') {
          await replaceResource(config.spaceId, resourceModalResourceId, fd);
        } else {
          await createResource(config.spaceId, fd);
        }
        form.reset();
        configureResourceModal({ mode: 'create', type: 'text' });
        setResourceModal(false);
        if (hint) hint.style.display = 'none';
        if (btn) btn.disabled = false;
        await refreshPageFromServer();
      } catch (err) {
        toast(err instanceof Error ? err.message : String(err || 'upload failed'));
        if (btn) btn.disabled = false;
        if (hint) hint.style.display = 'none';
      }
    });

    document.getElementById('insp-message-form')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (stateRef.current.waiting || stateRef.current.publishing) return;
      const input = document.getElementById('insp-message-input') as HTMLTextAreaElement | null;
      const content = String(input?.value || '').trim();
      if (!content) { toast('Enter a message'); return; }
      if (input) input.value = '';
      await submitTurn(content);
    });
    document.getElementById('insp-title-suggest')?.addEventListener('click', () => { if (!stateRef.current.waiting && !stateRef.current.publishing) void submitTurn('/summary_title'); });
    document.getElementById('insp-generate-draft')?.addEventListener('click', () => { if (!stateRef.current.waiting && !stateRef.current.publishing) void submitTurn('/plan'); });
    document.getElementById('insp-resource-sync')?.addEventListener('click', async () => {
      if (stateRef.current.waiting || stateRef.current.publishing) return;
      try { await syncResources(config.spaceId); window.location.reload(); } catch (err) { toast(err instanceof Error ? err.message : String(err || 'sync failed')); }
    });
    document.getElementById('insp-close-space')?.addEventListener('click', async () => {
      if (stateRef.current.publishing) return;
      try { await closeSpace(config.spaceId); window.location.reload(); } catch (err) { toast(err instanceof Error ? err.message : String(err || 'request failed')); }
    });
    document.getElementById('insp-reopen-space')?.addEventListener('click', async () => {
      if (stateRef.current.publishing) return;
      try { await reopenSpace(config.spaceId); window.location.reload(); } catch (err) { toast(err instanceof Error ? err.message : String(err || 'request failed')); }
    });
    document.getElementById('insp-delete-space')?.addEventListener('click', async () => {
      if (stateRef.current.publishing || !window.confirm('Delete this unpublished Inspiration space?')) return;
      try { await deleteSpace(config.spaceId); window.location.href = '/inspirations'; } catch (err) { toast(err instanceof Error ? err.message : String(err || 'delete failed')); }
    });
    document.getElementById('insp-fork-space')?.addEventListener('click', async () => {
      if (stateRef.current.publishing) return;
      const title = window.prompt('Fork title', config.forkTitle);
      if (title === null) return;
      try {
        const data = await forkSpace(config.spaceId, { title: String(title || '').trim(), include_all_resources: true });
        if (data.item?.id) window.location.href = `/inspirations/${data.item.id}`;
      } catch (err) { toast(err instanceof Error ? err.message : String(err || 'fork failed')); }
    });

    document.querySelectorAll<HTMLElement>('[data-resource-toggle]').forEach((btn) => {
      syncToggleButton(btn);
      btn.addEventListener('click', () => {
        const preview = btn.closest('.pad')?.querySelector<HTMLElement>('[data-resource-preview]');
        if (!preview) return;
        preview.classList.toggle('collapsed');
        syncToggleButton(btn);
      });
    });
    document.querySelectorAll<HTMLImageElement>('[data-resource-image]').forEach((img) => {
      const sync = () => syncToggleButton(img.closest('.pad')?.querySelector<HTMLElement>('[data-resource-toggle]') || null);
      if (img.complete) sync(); else img.addEventListener('load', sync, { once: true });
    });
    document.querySelectorAll<HTMLElement>('[data-resource-send]').forEach((btn) => btn.addEventListener('click', () => appendResourceReference(btn.getAttribute('data-resource-send') || '', resourceCardFromNode(btn))));
    document.querySelectorAll<HTMLElement>('[data-resource-edit]').forEach((btn) => btn.addEventListener('click', () => {
      if ((btn as HTMLButtonElement).disabled) return;
      const card = resourceCardFromNode(btn);
      const resourceType = String(card?.dataset.resourceType || 'text');
      configureResourceModal({ mode: 'edit', resourceId: Number(card?.dataset.resourceId || 0), type: resourceType === 'summary' ? 'text' : resourceType, name: resourceCardValue(card, '[data-resource-name]'), text: resourceType === 'url' ? '' : resourceCardValue(card, '[data-resource-text]'), url: resourceType === 'url' ? resourceCardValue(card, '[data-resource-url]') : '' });
      setResourceModal(true);
    }));
    document.querySelectorAll<HTMLElement>('[data-resource-replace]').forEach((btn) => btn.addEventListener('click', () => {
      const card = resourceCardFromNode(btn);
      configureResourceModal({ mode: 'replace', resourceId: Number(card?.dataset.resourceId || 0), type: 'image', name: resourceCardValue(card, '[data-resource-name]') });
      setResourceModal(true);
    }));
    document.querySelectorAll<HTMLElement>('[data-resource-copy]').forEach((btn) => btn.addEventListener('click', () => void copyResourceContent(resourceCardFromNode(btn))));
    document.querySelectorAll<HTMLElement>('[data-resource-delete]').forEach((btn) => btn.addEventListener('click', async () => {
      if ((btn as HTMLButtonElement).disabled || stateRef.current.waiting || stateRef.current.publishing) return;
      const card = resourceCardFromNode(btn);
      const resourceId = Number(card?.dataset.resourceId || 0);
      const resourceType = String(card?.dataset.resourceType || 'resource');
      if (!resourceId || !window.confirm(`Delete this ${resourceType} resource?`)) return;
      try { await deleteResource(config.spaceId, resourceId); await refreshPageFromServer(); } catch (err) { toast(err instanceof Error ? err.message : String(err || 'delete failed')); }
    }));
    document.querySelectorAll<HTMLElement>('.insp-draft-cancel-btn').forEach((btn) => btn.addEventListener('click', () => {
      btn.closest('.msg-row')?.remove();
      const terminalDrafts = document.querySelector('.terminal-drafts');
      if (terminalDrafts && !terminalDrafts.querySelector('.msg-row')) terminalDrafts.remove();
    }));
    document.querySelectorAll<HTMLFormElement>('.insp-publish-form').forEach((form) => form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (stateRef.current.waiting || stateRef.current.publishing || !window.confirm('Publish this draft and lock this Inspiration space?')) return;
      const fd = new FormData(form);
      const btn = form.querySelector<HTMLButtonElement>('.insp-publish-btn');
      setPublishingUI(true, btn);
      try {
        await publishDraft(config.spaceId, { draft_id: Number(fd.get('draft_id') || 0), due_date: String(fd.get('due_date') || '').trim() });
        scheduleBusyPoll(350);
      } catch (err) {
        setPublishingUI(false, btn);
        toast(err instanceof Error ? err.message : String(err || 'publish failed'));
      }
    }));

    mountTerminal();
    setWaitingUI(stateRef.current.waiting);
    setPublishingUI(stateRef.current.publishing);
  }, [config.forkTitle, config.isTerminalMode, config.resourceIds, config.spaceId, refreshPageFromServer, scheduleBusyPoll, setPublishingUI, setWaitingUI]);

  bindUIRef.current = bindUI;

  useEffect(() => {
    syncStateFromRoot();
    bindUI();
    scrollChat();
    if (stateRef.current.waiting || stateRef.current.publishing) scheduleBusyPoll(900);
    return () => {
      if (stateRef.current.busyPollTimer) window.clearTimeout(stateRef.current.busyPollTimer);
    };
  }, [bindUI, scheduleBusyPoll, scrollChat, syncStateFromRoot]);

  return null;
}

const mount = document.getElementById('inspiration-space-react-root');
if (mount) {
  const config = JSON.parse(mount.getAttribute('data-config') || '{}') as InspirationConfig;
  createRoot(mount).render(<InspirationSpaceController config={config} />);
}

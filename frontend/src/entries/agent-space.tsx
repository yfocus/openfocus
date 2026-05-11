/* SPDX-License-Identifier: Apache-2.0 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';

type AgentSpaceConfig = {
  spaceId: number;
  taskPublicId: string;
  rootPath: string;
};

type FileEntry = {
  name: string;
  rel_path: string;
  kind: string;
  size?: number;
  mtime?: number;
};

type PreviewState = {
  path?: string;
  name?: string;
  scrollTop?: number;
  topLine?: number;
  ts?: number;
};

function toast(message: string): void {
  if (typeof window.toast === 'function') window.toast(message);
}

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(text || `HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

function clamp(value: number, minValue: number, maxValue: number): number {
  if (!Number.isFinite(value)) return minValue;
  if (value < minValue) return minValue;
  if (value > maxValue) return maxValue;
  return value;
}

function currentPxVar(el: HTMLElement, name: string, fallback: number): number {
  try {
    const value = getComputedStyle(el).getPropertyValue(name).trim();
    if (value.endsWith('px')) return Number(value.slice(0, -2)) || fallback;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  } catch (_) {
    return fallback;
  }
}

function guessNameFromPath(relPath: string): string {
  const idx = relPath.lastIndexOf('/');
  return idx >= 0 ? relPath.slice(idx + 1) : relPath;
}

function isLikelyImage(name: string): boolean {
  const lower = name.toLowerCase();
  return ['.png', '.jpg', '.jpeg', '.gif', '.webp'].some((suffix) => lower.endsWith(suffix));
}

function previewStateKey(spaceId: number): string {
  return `openfocus.agent_space.preview.${String(spaceId)}`;
}

function loadPreviewState(spaceId: number): PreviewState | null {
  try {
    const raw = localStorage.getItem(previewStateKey(spaceId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PreviewState;
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch (_) {
    return null;
  }
}

function savePreviewState(spaceId: number, state: PreviewState): void {
  try {
    localStorage.setItem(previewStateKey(spaceId), JSON.stringify(state || {}));
  } catch (_) {
    // ignore storage failures
  }
}

function layoutStateKey(spaceId: number): string {
  return `openfocus.agent_space.layout.${String(spaceId)}`;
}

function loadLayoutState(spaceId: number): { filesW?: number; termW?: number } | null {
  try {
    const raw = localStorage.getItem(layoutStateKey(spaceId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { filesW?: number; termW?: number };
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch (_) {
    return null;
  }
}

function saveLayoutState(spaceId: number, state: { filesW: number; termW: number; ts: number }): void {
  try {
    localStorage.setItem(layoutStateKey(spaceId), JSON.stringify(state));
  } catch (_) {
    // ignore storage failures
  }
}

function FileTreeNode({ entry, spaceId, depth, onOpenFile }: { entry: FileEntry; spaceId: number; depth: number; onOpenFile: (path: string, name: string) => void }) {
  const [open, setOpen] = useState(depth === 0);
  const [loaded, setLoaded] = useState(false);
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (entry.kind !== 'dir' || !open || loaded) return;
    let cancelled = false;
    setError('');
    setLoading(true);
    fetchJson<{ entries?: FileEntry[] }>(`/api/agent_spaces/${spaceId}/files/list?path=${encodeURIComponent(entry.rel_path || '')}`)
      .then((data) => {
        if (cancelled) return;
        setEntries(Array.isArray(data.entries) ? data.entries : []);
        setLoaded(true);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(`加载失败：${err instanceof Error ? err.message : String(err)}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [entry.kind, entry.rel_path, loaded, open, spaceId]);

  const marginLeft = `${Math.max(0, Math.min(depth, 16)) * 12}px`;
  if (entry.kind === 'dir') {
    return (
      <details style={{ marginLeft }} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
        <summary style={{ cursor: 'pointer' }}>📁 {entry.name}</summary>
        {loading ? <div className="muted">加载中…</div> : null}
        {error ? <div className="muted">{error}</div> : null}
        {loaded && !entries.length ? <div className="muted">—</div> : null}
        {entries.map((child) => (
          <FileTreeNode key={`${child.kind}:${child.rel_path}`} entry={child} spaceId={spaceId} depth={depth + 1} onOpenFile={onOpenFile} />
        ))}
      </details>
    );
  }
  return (
    <a
      href="#"
      style={{ display: 'block', padding: '2px 0', marginLeft }}
      onClick={(event) => {
        event.preventDefault();
        onOpenFile(entry.rel_path || '', entry.name || '');
      }}
    >
      📄 {entry.name}
    </a>
  );
}

function FileTree({ spaceId, onOpenFile }: { spaceId: number; onOpenFile: (path: string, name: string) => void }) {
  const [reloadKey, setReloadKey] = useState(0);
  const rootEntry = useMemo<FileEntry>(() => ({ name: 'workspace', rel_path: '', kind: 'dir' }), [reloadKey]);
  return (
    <div>
      <button type="button" className="btn-ghost" style={{ marginBottom: 8 }} onClick={() => setReloadKey((value) => value + 1)}>
        Refresh
      </button>
      <FileTreeNode key={reloadKey} entry={rootEntry} spaceId={spaceId} depth={0} onOpenFile={onOpenFile} />
    </div>
  );
}

function CodePreview({ content }: { content: string }) {
  const lines = useMemo(() => String(content || '').split(/\r?\n/), [content]);
  return (
    <div className="codebox">
      {lines.map((line, idx) => (
        <div className="code-line" key={idx}>
          <span className="code-ln">{idx + 1}</span>
          <span className="code-tx">{line}</span>
        </div>
      ))}
    </div>
  );
}

function AgentSpaceApp({ config }: { config: AgentSpaceConfig }) {
  const splitRef = useRef<HTMLDivElement | null>(null);
  const previewScrollRef = useRef<HTMLDivElement | null>(null);
  const previewContentRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<HTMLDivElement | null>(null);
  const [preview, setPreview] = useState<{ path: string; name: string; content: string; imageUrl: string; loading: boolean; error: string }>(() => ({
    path: '',
    name: '',
    content: '',
    imageUrl: '',
    loading: false,
    error: '',
  }));

  const openPreview = useCallback(
    async (relPath: string, name: string) => {
      const previous = loadPreviewState(config.spaceId);
      const same = previous && String(previous.path || '') === String(relPath || '');
      savePreviewState(config.spaceId, {
        path: relPath,
        name,
        scrollTop: same ? Number(previous?.scrollTop || 0) : 0,
        topLine: same ? Number(previous?.topLine || 1) : 1,
        ts: Date.now(),
      });
      const displayName = name || guessNameFromPath(relPath);
      setPreview({ path: relPath, name: displayName, content: '', imageUrl: '', loading: true, error: '' });
      try {
        if (isLikelyImage(displayName)) {
          const imageUrl = `/api/agent_spaces/${config.spaceId}/files/raw?path=${encodeURIComponent(relPath)}`;
          setPreview({ path: relPath, name: displayName, content: '', imageUrl, loading: false, error: '' });
          requestAnimationFrame(() => {
            if (previewScrollRef.current) previewScrollRef.current.scrollTop = 0;
          });
          return;
        }
        const data = await fetchJson<{ content?: string }>(`/api/agent_spaces/${config.spaceId}/files/read?path=${encodeURIComponent(relPath)}`);
        setPreview({ path: relPath, name: displayName, content: String(data.content || ''), imageUrl: '', loading: false, error: '' });
      } catch (err) {
        setPreview({ path: relPath, name: displayName, content: '', imageUrl: '', loading: false, error: `预览失败：${err instanceof Error ? err.message : String(err)}` });
      }
    },
    [config.spaceId],
  );

  useEffect(() => {
    if (!preview.path || preview.loading || preview.error || preview.imageUrl) return;
    const state = loadPreviewState(config.spaceId);
    if (!state || String(state.path || '') !== String(preview.path || '')) return;
    const apply = () => {
      const scroller = previewScrollRef.current;
      const box = previewContentRef.current?.querySelector('.codebox');
      const firstLine = box?.querySelector('.code-line') as HTMLElement | null;
      const lineHeight = firstLine ? firstLine.getBoundingClientRect().height || firstLine.offsetHeight || 0 : 0;
      if (!scroller) return;
      const topLine = Number(state.topLine || 0);
      if (lineHeight > 0 && topLine > 1) scroller.scrollTop = Math.max(0, Math.floor((topLine - 1) * lineHeight));
      else if (typeof state.scrollTop === 'number' && state.scrollTop > 0) scroller.scrollTop = Math.max(0, Math.floor(state.scrollTop));
    };
    requestAnimationFrame(() => requestAnimationFrame(apply));
    const timeout = window.setTimeout(apply, 120);
    return () => window.clearTimeout(timeout);
  }, [config.spaceId, preview.error, preview.imageUrl, preview.loading, preview.path]);

  useEffect(() => {
    const state = loadPreviewState(config.spaceId);
    if (!state?.path) return;
    void openPreview(String(state.path || ''), String(state.name || '') || guessNameFromPath(String(state.path || '')));
  }, [config.spaceId, openPreview]);

  useEffect(() => {
    const scroller = previewScrollRef.current;
    if (!scroller) return;
    let timer = 0;
    const handler = () => {
      if (timer) return;
      timer = window.setTimeout(() => {
        timer = 0;
        const state = loadPreviewState(config.spaceId) || {};
        const path = String(state.path || '');
        if (!path) return;
        let topLine = Number(state.topLine || 1);
        const box = previewContentRef.current?.querySelector('.codebox');
        const firstLine = box?.querySelector('.code-line') as HTMLElement | null;
        const lineHeight = firstLine ? firstLine.getBoundingClientRect().height || firstLine.offsetHeight || 0 : 0;
        if (lineHeight > 0) topLine = Math.max(1, Math.floor(scroller.scrollTop / lineHeight) + 1);
        savePreviewState(config.spaceId, { path, name: String(state.name || ''), scrollTop: Number(scroller.scrollTop || 0), topLine, ts: Date.now() });
      }, 180);
    };
    scroller.addEventListener('scroll', handler, { passive: true });
    window.addEventListener('pageshow', handler);
    const visibilityHandler = () => {
      if (document.visibilityState === 'hidden') handler();
    };
    document.addEventListener('visibilitychange', visibilityHandler);
    return () => {
      scroller.removeEventListener('scroll', handler);
      window.removeEventListener('pageshow', handler);
      document.removeEventListener('visibilitychange', visibilityHandler);
      if (timer) window.clearTimeout(timer);
    };
  }, [config.spaceId]);

  useEffect(() => {
    const root = splitRef.current;
    if (!root) return;
    const state = loadLayoutState(config.spaceId);
    if (state?.filesW) root.style.setProperty('--files-w', `${Math.floor(Number(state.filesW))}px`);
    if (state?.termW) root.style.setProperty('--term-w', `${Math.floor(Number(state.termW))}px`);
  }, [config.spaceId]);

  const startDrag = useCallback(
    (side: 'left' | 'right', event: React.MouseEvent<HTMLDivElement> | React.TouchEvent<HTMLDivElement>) => {
      if (window.matchMedia?.('(max-width: 1100px)').matches) return;
      const root = splitRef.current;
      if (!root) return;
      const startX = 'clientX' in event ? event.clientX : event.touches[0]?.clientX || 0;
      const startFilesW = currentPxVar(root, '--files-w', 340);
      const startTermW = currentPxVar(root, '--term-w', 420);
      const splitters = Array.from(root.querySelectorAll('.agent-space-splitter'));
      splitters.forEach((splitter) => splitter.classList.toggle('dragging', (splitter as HTMLElement).dataset.split === side));
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      const onMove = (ev: MouseEvent | TouchEvent) => {
        const x = ev instanceof MouseEvent ? ev.clientX : ev.touches[0]?.clientX || 0;
        const dx = x - startX;
        if (ev.cancelable) ev.preventDefault();
        const total = Math.floor(root.getBoundingClientRect().width || 0);
        if (!(total > 0)) return;
        const minFiles = 220;
        const minPreview = 320;
        const minTerm = 320;
        const available = total - 20;
        if (available <= minFiles + minPreview + minTerm) return;
        const maxFiles = available - minPreview - minTerm;
        const maxTerm = available - minPreview - minFiles;
        const nextFiles = side === 'left' ? clamp(startFilesW + dx, minFiles, maxFiles) : startFilesW;
        const nextTerm = side === 'right' ? clamp(startTermW - dx, minTerm, maxTerm) : startTermW;
        root.style.setProperty('--files-w', `${Math.floor(nextFiles)}px`);
        root.style.setProperty('--term-w', `${Math.floor(nextTerm)}px`);
        window.dispatchEvent(new CustomEvent('openfocus:agent-space-layout-changed', { detail: { spaceId: config.spaceId, ts: Date.now() } }));
      };
      const endDrag = () => {
        splitters.forEach((splitter) => splitter.classList.remove('dragging'));
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', endDrag);
        document.removeEventListener('touchmove', onMove);
        document.removeEventListener('touchend', endDrag);
        saveLayoutState(config.spaceId, {
          filesW: currentPxVar(root, '--files-w', startFilesW),
          termW: currentPxVar(root, '--term-w', startTermW),
          ts: Date.now(),
        });
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', endDrag);
      document.addEventListener('touchmove', onMove, { passive: false });
      document.addEventListener('touchend', endDrag);
      event.preventDefault();
    },
    [config.spaceId],
  );

  useEffect(() => {
    const el = terminalRef.current;
    if (!el || !window.OpenFocusRemoteTerminal?.mount) return;
    try {
      window.OpenFocusRemoteTerminal.mount(el, { spaceId: config.spaceId, taskPublicId: config.taskPublicId });
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error(err);
      window.alert(`终端初始化失败：${err instanceof Error ? err.message : String(err)}`);
    }
  }, [config.spaceId, config.taskPublicId]);

  useEffect(() => {
    const copyButton = document.getElementById('space-copy-task');
    const cleanupButton = document.getElementById('space-release');
    const copyTaskId = async () => {
      try {
        await navigator.clipboard.writeText(config.taskPublicId);
        toast('已复制');
      } catch (_) {
        toast('复制失败');
      }
    };
    const releaseSpace = async () => {
      if (!window.confirm('确认释放该 AgentSpace？（只会删除 OpenFocus 侧记录，不会删除你的本地文件）')) return;
      try {
        const response = await fetch(`/api/tasks/${encodeURIComponent(config.taskPublicId)}/agent_space`, { method: 'DELETE' });
        if (!response.ok) throw new Error(await response.text().catch(() => `HTTP ${response.status}`));
        toast('已释放');
        window.location.href = `/goals?task=${encodeURIComponent(config.taskPublicId)}`;
      } catch (err) {
        toast('释放失败');
        window.alert(`释放失败：${err instanceof Error ? err.message : String(err)}`);
      }
    };
    copyButton?.addEventListener('click', copyTaskId);
    cleanupButton?.addEventListener('click', releaseSpace);
    return () => {
      copyButton?.removeEventListener('click', copyTaskId);
      cleanupButton?.removeEventListener('click', releaseSpace);
    };
  }, [config.taskPublicId]);

  return (
    <>
      <div ref={splitRef} id="agent-space-split" className="agent-space-split" style={{ flex: '1 1 0', minHeight: 0, height: 'auto' }}>
        <div className="panel" style={{ height: '100%', padding: 0 }}>
          <div style={{ height: '100%', minHeight: 0, display: 'flex', flexDirection: 'column' }}>
            <div className="pad" style={{ padding: 14, flex: '0 0 auto' }}>
              <div className="muted" style={{ fontSize: 12 }} title={config.rootPath}>
                {config.rootPath}
              </div>
            </div>
            <div className="divider" />
            <div className="col-scroll pad" style={{ flex: '1 1 auto', minHeight: 0, height: 'auto', padding: 12 }}>
              <FileTree spaceId={config.spaceId} onOpenFile={openPreview} />
            </div>
          </div>
        </div>

        <div className="agent-space-splitter" data-split="left" title="拖拽调整 FILES / PREVIEW 宽度" onMouseDown={(event) => startDrag('left', event)} onTouchStart={(event) => startDrag('left', event)} onDoubleClick={() => {
          const root = splitRef.current;
          if (!root) return;
          root.style.setProperty('--files-w', '340px');
          root.style.setProperty('--term-w', '420px');
          saveLayoutState(config.spaceId, { filesW: 340, termW: 420, ts: Date.now() });
        }} />

        <div className="panel" style={{ height: '100%', padding: 0 }}>
          <div style={{ height: '100%', minHeight: 0, display: 'flex', flexDirection: 'column' }}>
            <div className="pad" style={{ padding: 14, flex: '0 0 auto' }}>
              <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10 }}>
                <div className="muted" style={{ fontSize: 12 }}>{preview.name || '—'}</div>
              </div>
            </div>
            <div className="divider" />
            <div ref={previewScrollRef} className="col-scroll pad" style={{ flex: '1 1 auto', minHeight: 0, height: 'auto', padding: 12 }}>
              <div ref={previewContentRef} className={preview.path ? '' : 'muted'}>
                {preview.loading ? <><span className="spin" /> <span className="muted">加载中…</span></> : null}
                {preview.error ? preview.error : null}
                {!preview.loading && !preview.error && preview.imageUrl ? <img src={preview.imageUrl} style={{ maxWidth: '100%', height: 'auto' }} /> : null}
                {!preview.loading && !preview.error && preview.content ? <CodePreview content={preview.content} /> : null}
                {!preview.path ? '选择一个文件预览（代码 / Markdown / 图片）。' : null}
              </div>
            </div>
          </div>
        </div>

        <div className="agent-space-splitter" data-split="right" title="拖拽调整 PREVIEW / TERMINAL 宽度" onMouseDown={(event) => startDrag('right', event)} onTouchStart={(event) => startDrag('right', event)} onDoubleClick={() => {
          const root = splitRef.current;
          if (!root) return;
          root.style.setProperty('--files-w', '340px');
          root.style.setProperty('--term-w', '420px');
          saveLayoutState(config.spaceId, { filesW: 340, termW: 420, ts: Date.now() });
        }} />

        <div className="panel" style={{ height: '100%', padding: 0 }}>
          <div style={{ height: '100%', minHeight: 0, display: 'flex', flexDirection: 'column' }}>
            <div className="pad" style={{ flex: '1 1 auto', minHeight: 0, minWidth: 0, height: 'auto', padding: 12 }}>
              <div ref={terminalRef} id="remote-terminal" style={{ height: '100%', minHeight: 0 }} />
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

const mount = document.getElementById('agent-space-react-root');
if (mount) {
  const config = JSON.parse(mount.getAttribute('data-config') || '{}') as AgentSpaceConfig;
  createRoot(mount).render(<AgentSpaceApp config={config} />);
}

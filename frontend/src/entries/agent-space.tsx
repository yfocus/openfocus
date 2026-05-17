/* SPDX-License-Identifier: Apache-2.0 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { basicSetup, EditorView } from 'codemirror';
import { type Extension, EditorState, StateEffect, StateField } from '@codemirror/state';
import { Decoration, type DecorationSet } from '@codemirror/view';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { tags } from '@lezer/highlight';
import { cpp } from '@codemirror/lang-cpp';
import { css } from '@codemirror/lang-css';
import { go } from '@codemirror/lang-go';
import { html } from '@codemirror/lang-html';
import { java } from '@codemirror/lang-java';
import { javascript } from '@codemirror/lang-javascript';
import { json } from '@codemirror/lang-json';
import { lezer } from '@codemirror/lang-lezer';
import { markdown } from '@codemirror/lang-markdown';
import { php } from '@codemirror/lang-php';
import { python } from '@codemirror/lang-python';
import { rust } from '@codemirror/lang-rust';
import { sql } from '@codemirror/lang-sql';
import { xml } from '@codemirror/lang-xml';
import { listFiles, rawFileUrl, readFile, releaseTaskAgentSpace } from '../api/agentSpaces';
import type { FileEntry } from '../types/openfocus';

type AgentSpaceConfig = {
  spaceId: number;
  taskPublicId: string;
  rootPath: string;
  agentPrefix?: string;
  startAgentCommand?: string;
};

type TerminalApi = {
  injectPromptToTerminal?: (
    text: string,
    options?: { bracketedPaste?: boolean; submit?: boolean; focus?: boolean },
  ) => Promise<boolean>;
};

type PreviewState = {
  path?: string;
  name?: string;
  scrollTop?: number;
  topLine?: number;
  targetLine?: number;
  targetColumn?: number;
  targetNonce?: number;
  ts?: number;
};

type PreviewTarget = {
  line?: number;
  column?: number;
};

type PreviewViewState = {
  path: string;
  name: string;
  content: string;
  imageUrl: string;
  loading: boolean;
  error: string;
  targetLine?: number;
  targetColumn?: number;
  targetNonce?: number;
};

type AgentContextMenuItem = {
  label: string;
  action: () => void | Promise<void>;
};

type AgentContextMenuState = {
  x: number;
  y: number;
  items: AgentContextMenuItem[];
};

type TerminalLinkOpenMessage = {
  type?: unknown;
  href?: unknown;
  text?: unknown;
  path?: unknown;
  line?: unknown;
  column?: unknown;
  terminalId?: unknown;
};

type PreviewSelectionState = {
  text: string;
  fromLine?: number;
  toLine?: number;
};

function toast(message: string): void {
  if (typeof window.toast === 'function') window.toast(message);
}

function clamp(value: number, minValue: number, maxValue: number): number {
  if (!Number.isFinite(value)) return minValue;
  if (value < minValue) return minValue;
  if (value > maxValue) return maxValue;
  return value;
}

function positiveInt(value: unknown): number | undefined {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 1) return undefined;
  return Math.floor(n);
}

function cleanString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function shellQuotePath(path: string): string {
  const value = String(path || '.');
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function formatAgentFileReference(relPath: string, line?: number): string {
  const path = String(relPath || '').trim();
  if (!path) return '';
  const safeLine = positiveInt(line);
  return `@${path}${safeLine ? `#L${safeLine}` : ''}`;
}

function stripFileReference(raw: string): string {
  let s = String(raw || '').trim();
  while (s.length >= 2 && "'\"`".includes(s[0]) && s[s.length - 1] === s[0]) s = s.slice(1, -1).trim();
  const pairs: Record<string, string> = { '(': ')', '[': ']', '{': '}', '<': '>' };
  let changed = true;
  while (changed && s.length >= 2) {
    changed = false;
    const end = pairs[s[0]];
    if (end && s[s.length - 1] === end) {
      s = s.slice(1, -1).trim();
      changed = true;
    }
  }
  return s.replace(/[.,;]+$/g, '');
}

function stripAgentReferencePrefix(raw: string): string {
  const value = stripFileReference(raw);
  return value.startsWith('@') && value.length > 1 ? stripFileReference(value.slice(1)) : value;
}

function isHttpUrl(value: string): boolean {
  return /^https?:\/\//i.test(String(value || '').trim());
}

function extractFileReferenceCandidates(raw: string): string[] {
  const value = String(raw || '');
  const candidates: string[] = [];
  const pattern = /@?file:\/\/[^\s<>"'`)\]}]+|@?(?:\.{1,2}\/|\/|[A-Za-z0-9_.@+-]+\/)[^\s<>"'`)\]}]+|@?[A-Za-z0-9_.@+-]+\.[A-Za-z0-9_+-]{1,16}(?::\d+){0,2}(?:#L\d+(?:C\d+)?)?/gi;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(value))) {
    const candidate = stripFileReference(match[0] || '');
    if (candidate) candidates.push(candidate);
  }
  return Array.from(new Set(candidates));
}

function parseSingleFileReference(raw: string): { path: string; line?: number; column?: number } | null {
  let value = stripAgentReferencePrefix(raw);
  if (!value || isHttpUrl(value)) return null;

  let cameFromFileUrl = false;
  if (/^file:\/\//i.test(value)) {
    cameFromFileUrl = true;
    try {
      const url = new URL(value);
      value = decodeURIComponent(url.pathname || '');
    } catch (_) {
      value = value.replace(/^file:\/\//i, '');
    }
  }

  let line: number | undefined;
  let column: number | undefined;
  const anchorMatch = value.match(/#L(\d+)(?:C(\d+))?$/i);
  if (anchorMatch) {
    line = positiveInt(anchorMatch[1]);
    column = positiveInt(anchorMatch[2]);
    value = value.slice(0, anchorMatch.index).trim();
  } else {
    const suffixMatch = value.match(/:(\d+)(?::(\d+))?$/);
    if (suffixMatch) {
      line = positiveInt(suffixMatch[1]);
      column = positiveInt(suffixMatch[2]);
      value = value.slice(0, suffixMatch.index).trim();
    }
  }

  const path = stripAgentReferencePrefix(value);
  if (!path) return null;
  if (!cameFromFileUrl && !(/^\.{1,2}\//.test(path) || path.startsWith('/') || path.includes('/') || /[A-Za-z0-9_.@+-]+\.[A-Za-z0-9_+-]{1,16}$/.test(path))) return null;
  return { path, line, column };
}

function parseTerminalFileReference(raw: string): { path: string; line?: number; column?: number } | null {
  const directCandidate = stripFileReference(raw);
  const direct = /\s/.test(directCandidate) ? null : parseSingleFileReference(directCandidate);
  if (direct) return direct;
  for (const candidate of extractFileReferenceCandidates(raw)) {
    const parsed = parseSingleFileReference(candidate);
    if (parsed) return parsed;
  }
  return null;
}

function normalizeAbsolutePath(value: string): string | null {
  const raw = String(value || '').replace(/\\/g, '/');
  if (!raw.startsWith('/')) return null;
  const parts: string[] = [];
  for (const part of raw.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (!parts.length) return null;
      parts.pop();
      continue;
    }
    if (part.includes('\0')) return null;
    parts.push(part);
  }
  return `/${parts.join('/')}`;
}

function normalizeRelativePath(value: string): string | null {
  const raw = String(value || '').replace(/\\/g, '/');
  if (!raw || raw.startsWith('/')) return null;
  const parts: string[] = [];
  for (const part of raw.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (!parts.length) return null;
      parts.pop();
      continue;
    }
    if (part.includes('\0')) return null;
    parts.push(part);
  }
  return parts.length ? parts.join('/') : null;
}

function workspaceRelativePath(candidatePath: string, rootPath: string): string | null {
  const candidate = stripAgentReferencePrefix(candidatePath);
  if (!candidate) return null;
  if (candidate.startsWith('/')) {
    const root = normalizeAbsolutePath(rootPath);
    const abs = normalizeAbsolutePath(candidate);
    if (!root || !abs) return null;
    if (abs === root) return null;
    if (!abs.startsWith(`${root}/`)) return null;
    return normalizeRelativePath(abs.slice(root.length + 1));
  }
  return normalizeRelativePath(candidate);
}

function fileReferenceFromTerminalMessage(data: TerminalLinkOpenMessage, rootPath: string): { relPath: string; line?: number; column?: number } | null {
  const rawCandidates = [
    cleanString(data.path),
    cleanString(data.href),
    cleanString(data.text),
    ...extractFileReferenceCandidates(cleanString(data.text)),
    ...extractFileReferenceCandidates(cleanString(data.href)),
  ].filter(Boolean);
  const candidates = Array.from(new Set(rawCandidates));
  for (const candidate of candidates) {
    const parsed = parseTerminalFileReference(candidate);
    if (!parsed) continue;
    const relPath = workspaceRelativePath(parsed.path, rootPath);
    if (!relPath) continue;
    return {
      relPath,
      line: positiveInt(data.line) || parsed.line,
      column: positiveInt(data.column) || parsed.column,
    };
  }
  return null;
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

function fileExtension(name: string): string {
  const cleanName = String(name || '').toLowerCase().split('?')[0] || '';
  const idx = cleanName.lastIndexOf('.');
  return idx >= 0 ? cleanName.slice(idx + 1) : '';
}

function languageExtension(name: string): Extension[] {
  const lower = String(name || '').toLowerCase();
  const ext = fileExtension(lower);
  if (['js', 'jsx', 'mjs', 'cjs'].includes(ext)) return [javascript({ jsx: ext === 'jsx' })];
  if (['ts', 'tsx', 'mts', 'cts'].includes(ext)) return [javascript({ typescript: true, jsx: ext === 'tsx' })];
  if (['py', 'pyw'].includes(ext)) return [python()];
  if (['html', 'htm', 'jinja', 'jinja2'].includes(ext)) return [html()];
  if (['css'].includes(ext)) return [css()];
  if (['json', 'jsonc', 'map'].includes(ext)) return [json()];
  if (['md', 'markdown'].includes(ext)) return [markdown()];
  if (['xml', 'svg'].includes(ext)) return [xml()];
  if (['rs'].includes(ext)) return [rust()];
  if (['java'].includes(ext)) return [java()];
  if (['c', 'h', 'cc', 'cpp', 'cxx', 'hpp', 'hh'].includes(ext)) return [cpp()];
  if (['go'].includes(ext)) return [go()];
  if (['php'].includes(ext)) return [php()];
  if (['sql'].includes(ext)) return [sql()];
  if (['grammar'].includes(ext) || lower.endsWith('.grammar.terms')) return [lezer()];
  return [];
}

const openFocusHighlightStyle = HighlightStyle.define([
  { tag: tags.keyword, color: '#7c4dff' },
  { tag: [tags.name, tags.deleted, tags.character, tags.macroName], color: '#d7ffe9' },
  { tag: [tags.propertyName, tags.function(tags.variableName), tags.labelName], color: '#00e5ff' },
  { tag: [tags.color, tags.constant(tags.name), tags.standard(tags.name)], color: '#2bffb7' },
  { tag: [tags.definition(tags.name), tags.separator], color: '#ffd166' },
  { tag: [tags.typeName, tags.className, tags.number, tags.changed, tags.annotation, tags.modifier, tags.self, tags.namespace], color: '#ff9f7a' },
  { tag: [tags.operator, tags.operatorKeyword, tags.url, tags.escape, tags.regexp, tags.link], color: '#ff7ad9' },
  { tag: [tags.meta, tags.comment], color: 'rgba(215,255,233,0.46)' },
  { tag: tags.strong, fontWeight: '700' },
  { tag: tags.emphasis, fontStyle: 'italic' },
  { tag: tags.strikethrough, textDecoration: 'line-through' },
  { tag: tags.link, textDecoration: 'underline' },
  { tag: tags.heading, fontWeight: '700', color: '#ffd166' },
  { tag: [tags.atom, tags.bool, tags.special(tags.variableName)], color: '#2bffb7' },
  { tag: [tags.processingInstruction, tags.string, tags.inserted], color: '#a5ffcf' },
  { tag: tags.invalid, color: '#ff3b5c' },
]);

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

function FileTreeNode({
  entry,
  spaceId,
  depth,
  onOpenFile,
  onFileContextMenu,
}: {
  entry: FileEntry;
  spaceId: number;
  depth: number;
  onOpenFile: (path: string, name: string) => void;
  onFileContextMenu: (event: React.MouseEvent<HTMLElement>, entry: FileEntry) => void;
}) {
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
    listFiles(spaceId, entry.rel_path || '')
      .then((data) => {
        if (cancelled) return;
        setEntries(Array.isArray(data.entries) ? data.entries : []);
        setLoaded(true);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(`Failed to load: ${err instanceof Error ? err.message : String(err)}`);
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
        <summary style={{ cursor: 'pointer' }} onContextMenu={(event) => onFileContextMenu(event, entry)}>📁 {entry.name}</summary>
        {loading ? <div className="muted">Loading…</div> : null}
        {error ? <div className="muted">{error}</div> : null}
        {loaded && !entries.length ? <div className="muted">—</div> : null}
        {entries.map((child) => (
          <FileTreeNode key={`${child.kind}:${child.rel_path}`} entry={child} spaceId={spaceId} depth={depth + 1} onOpenFile={onOpenFile} onFileContextMenu={onFileContextMenu} />
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
      onContextMenu={(event) => onFileContextMenu(event, entry)}
    >
      📄 {entry.name}
    </a>
  );
}

function FileTree({
  spaceId,
  onOpenFile,
  onFileContextMenu,
}: {
  spaceId: number;
  onOpenFile: (path: string, name: string) => void;
  onFileContextMenu: (event: React.MouseEvent<HTMLElement>, entry: FileEntry) => void;
}) {
  const [reloadKey, setReloadKey] = useState(0);
  const rootEntry = useMemo<FileEntry>(() => ({ name: 'workspace', rel_path: '', kind: 'dir' }), [reloadKey]);
  return (
    <div>
      <button type="button" className="btn-ghost" style={{ marginBottom: 8 }} onClick={() => setReloadKey((value) => value + 1)}>
        Refresh
      </button>
      <FileTreeNode key={reloadKey} entry={rootEntry} spaceId={spaceId} depth={0} onOpenFile={onOpenFile} onFileContextMenu={onFileContextMenu} />
    </div>
  );
}

const targetLineEffect = StateEffect.define<number | null>();

const targetLineField = StateField.define<DecorationSet>({
  create() {
    return Decoration.none;
  },
  update(value, transaction) {
    let next = value.map(transaction.changes);
    for (const effect of transaction.effects) {
      if (!effect.is(targetLineEffect)) continue;
      if (effect.value === null) {
        next = Decoration.none;
      } else {
        next = Decoration.set([Decoration.line({ class: 'cm-openfocus-target-line' }).range(effect.value)]);
      }
    }
    return next;
  },
  provide: (field) => EditorView.decorations.from(field),
});

function selectedTextFromEditor(view: EditorView): PreviewSelectionState {
  const ranges = view.state.selection.ranges.filter((range) => !range.empty);
  if (!ranges.length) return { text: '' };
  const from = Math.min(...ranges.map((range) => range.from));
  const to = Math.max(...ranges.map((range) => range.to));
  return {
    text: ranges.map((range) => view.state.sliceDoc(range.from, range.to)).join('\n'),
    fromLine: view.state.doc.lineAt(from).number,
    toLine: view.state.doc.lineAt(to).number,
  };
}

function CodeMirrorPreview({
  content,
  name,
  onScroll,
  onSelectionChange,
  targetLine,
  targetColumn,
  targetNonce,
}: {
  content: string;
  name: string;
  onScroll: (scrollTop: number, topLine: number) => void;
  onSelectionChange: (selection: PreviewSelectionState) => void;
  targetLine?: number;
  targetColumn?: number;
  targetNonce?: number;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const scrollTimerRef = useRef<number>(0);
  const targetClearTimerRef = useRef<number>(0);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    onSelectionChange({ text: '' });

    const view = new EditorView({
      state: EditorState.create({
        doc: String(content || ''),
        extensions: [
          basicSetup,
          syntaxHighlighting(openFocusHighlightStyle),
          ...languageExtension(name),
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          targetLineField,
          EditorView.theme({
            '&': {
              height: '100%',
              color: 'var(--text)',
              backgroundColor: 'transparent',
              fontSize: '12px',
            },
            '.cm-scroller': {
              fontFamily: 'var(--mono)',
              lineHeight: '1.55',
              overflow: 'auto',
            },
            '.cm-content': {
              caretColor: 'transparent',
            },
            '.cm-line': {
              padding: '0 8px',
            },
            '.cm-gutters': {
              backgroundColor: 'transparent',
              color: 'rgba(215,255,233,0.40)',
              borderRight: '1px solid rgba(0,229,255,0.14)',
            },
            '.cm-activeLine': {
              backgroundColor: 'rgba(0,229,255,0.05)',
            },
            '.cm-activeLineGutter': {
              backgroundColor: 'rgba(0,229,255,0.06)',
              color: 'rgba(215,255,233,0.62)',
            },
            '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': {
              backgroundColor: 'rgba(0,229,255,0.22)',
            },
            '.cm-openfocus-target-line': {
              backgroundColor: 'rgba(255, 209, 102, 0.16)',
              outline: '1px solid rgba(255, 209, 102, 0.42)',
            },
            '&.cm-focused': {
              outline: 'none',
            },
          }),
          EditorView.domEventHandlers({
            scroll: (_event, currentView) => {
              if (scrollTimerRef.current) return;
              scrollTimerRef.current = window.setTimeout(() => {
                scrollTimerRef.current = 0;
                const scroller = currentView.scrollDOM;
                const top = Number(scroller.scrollTop || 0);
                const block = currentView.lineBlockAtHeight(top);
                const line = currentView.state.doc.lineAt(block.from).number;
                onScroll(top, line);
              }, 180);
            },
          }),
          EditorView.updateListener.of((update) => {
            if (update.selectionSet || update.docChanged) onSelectionChange(selectedTextFromEditor(update.view));
          }),
        ],
      }),
      parent: host,
    });

    viewRef.current = view;
    return () => {
      if (scrollTimerRef.current) window.clearTimeout(scrollTimerRef.current);
      if (targetClearTimerRef.current) window.clearTimeout(targetClearTimerRef.current);
      onSelectionChange({ text: '' });
      view.destroy();
      viewRef.current = null;
    };
  }, [content, name, onScroll, onSelectionChange]);

  useEffect(() => {
    const view = viewRef.current;
    const lineNumber = positiveInt(targetLine);
    if (!view || !lineNumber) return;
    const doc = view.state.doc;
    const safeLine = clamp(lineNumber, 1, Math.max(1, doc.lines));
    const line = doc.line(safeLine);
    const safeColumn = clamp(positiveInt(targetColumn) || 1, 1, Math.max(1, line.length + 1));
    const pos = line.from + safeColumn - 1;
    if (targetClearTimerRef.current) window.clearTimeout(targetClearTimerRef.current);
    view.dispatch({
      effects: [
        targetLineEffect.of(line.from),
        EditorView.scrollIntoView(pos, { y: 'center' }),
      ],
    });
    targetClearTimerRef.current = window.setTimeout(() => {
      const current = viewRef.current;
      if (current) current.dispatch({ effects: targetLineEffect.of(null) });
      targetClearTimerRef.current = 0;
    }, 1800);
  }, [content, targetColumn, targetLine, targetNonce]);

  return <div ref={hostRef} className="codebox cm-preview" />;
}

function AgentSpaceApp({ config }: { config: AgentSpaceConfig }) {
  const splitRef = useRef<HTMLDivElement | null>(null);
  const previewScrollRef = useRef<HTMLDivElement | null>(null);
  const previewContentRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<HTMLDivElement | null>(null);
  const terminalApiRef = useRef<TerminalApi | null>(null);
  const previewSelectionRef = useRef<PreviewSelectionState>({ text: '' });
  const [contextMenu, setContextMenu] = useState<AgentContextMenuState | null>(null);
  const [preview, setPreview] = useState<PreviewViewState>(() => ({
    path: '',
    name: '',
    content: '',
    imageUrl: '',
    loading: false,
    error: '',
  }));

  const openPreview = useCallback(
    async (relPath: string, name: string, target?: PreviewTarget) => {
      const targetLine = positiveInt(target?.line);
      const targetColumn = positiveInt(target?.column);
      const targetNonce = targetLine ? Date.now() : undefined;
      const previous = loadPreviewState(config.spaceId);
      const same = previous && String(previous.path || '') === String(relPath || '');
      savePreviewState(config.spaceId, {
        path: relPath,
        name,
        scrollTop: targetLine ? 0 : same ? Number(previous?.scrollTop || 0) : 0,
        topLine: targetLine || (same ? Number(previous?.topLine || 1) : 1),
        targetLine,
        targetColumn,
        targetNonce,
        ts: Date.now(),
      });
      const displayName = name || guessNameFromPath(relPath);
      setPreview({ path: relPath, name: displayName, content: '', imageUrl: '', loading: true, error: '', targetLine, targetColumn, targetNonce });
      try {
        if (isLikelyImage(displayName)) {
          const imageUrl = rawFileUrl(config.spaceId, relPath);
          setPreview({ path: relPath, name: displayName, content: '', imageUrl, loading: false, error: '', targetLine, targetColumn, targetNonce });
          requestAnimationFrame(() => {
            if (previewScrollRef.current) previewScrollRef.current.scrollTop = 0;
          });
          return;
        }
        const data = await readFile(config.spaceId, relPath);
        setPreview({ path: relPath, name: displayName, content: String(data.content || ''), imageUrl: '', loading: false, error: '', targetLine, targetColumn, targetNonce });
      } catch (err) {
        setPreview({ path: relPath, name: displayName, content: '', imageUrl: '', loading: false, error: `Preview failed: ${err instanceof Error ? err.message : String(err)}`, targetLine, targetColumn, targetNonce });
      }
    },
    [config.spaceId],
  );

  useEffect(() => {
    if (!preview.path || preview.loading || preview.error || preview.imageUrl) return;
    if (positiveInt(preview.targetLine)) return;
    const state = loadPreviewState(config.spaceId);
    if (!state || String(state.path || '') !== String(preview.path || '')) return;
    const apply = () => {
      const scroller = previewScrollRef.current;
      if (!scroller) return;
      const cmScroller = previewContentRef.current?.querySelector('.cm-scroller') as HTMLElement | null;
      const topLine = Number(state.topLine || 0);
      const scrollTop = Math.max(0, Math.floor(Number(state.scrollTop || 0)));
      if (cmScroller) cmScroller.scrollTop = scrollTop;
      else if (topLine > 1 || scrollTop > 0) scroller.scrollTop = scrollTop;
    };
    requestAnimationFrame(() => requestAnimationFrame(apply));
    const timeout = window.setTimeout(apply, 120);
    return () => window.clearTimeout(timeout);
  }, [config.spaceId, preview.error, preview.imageUrl, preview.loading, preview.path, preview.targetLine]);

  useEffect(() => {
    const state = loadPreviewState(config.spaceId);
    if (!state?.path) return;
    const targetLine = positiveInt(state.targetLine);
    void openPreview(
      String(state.path || ''),
      String(state.name || '') || guessNameFromPath(String(state.path || '')),
      targetLine ? { line: targetLine, column: positiveInt(state.targetColumn) } : undefined,
    );
  }, [config.spaceId, openPreview]);

  const savePreviewScroll = useCallback(
    (scrollTop: number, topLine: number) => {
      const state = loadPreviewState(config.spaceId) || {};
      const path = String(state.path || '');
      if (!path) return;
      savePreviewState(config.spaceId, { path, name: String(state.name || ''), scrollTop: Number(scrollTop || 0), topLine: Number(topLine || 1), ts: Date.now() });
    },
    [config.spaceId],
  );

  const updatePreviewSelection = useCallback((selection: PreviewSelectionState) => {
    previewSelectionRef.current = selection || { text: '' };
  }, []);

  const selectedPreviewReference = useCallback((): string => {
    const host = previewContentRef.current;
    const domSelection = window.getSelection?.();
    if (host && domSelection && !domSelection.isCollapsed) {
      const anchor = domSelection.anchorNode;
      const focus = domSelection.focusNode;
      if ((anchor && host.contains(anchor)) || (focus && host.contains(focus))) {
        const selected = domSelection.toString();
        if (selected.trim()) {
          const trackedLine = positiveInt(previewSelectionRef.current.fromLine);
          return formatAgentFileReference(preview.path, trackedLine);
        }
      }
    }
    const editorSelection = previewSelectionRef.current;
    return editorSelection.text.trim() ? formatAgentFileReference(preview.path, editorSelection.fromLine) : '';
  }, [preview.path]);

  const showContextMenu = useCallback((event: React.MouseEvent<HTMLElement>, items: AgentContextMenuItem[]) => {
    if (!items.length) return;
    event.preventDefault();
    event.stopPropagation();
    const menuWidth = 220;
    const menuHeight = 44 + items.length * 36;
    setContextMenu({
      x: clamp(event.clientX, 8, Math.max(8, window.innerWidth - menuWidth)),
      y: clamp(event.clientY, 8, Math.max(8, window.innerHeight - menuHeight)),
      items,
    });
  }, []);

  const sendToTerminal = useCallback(async (text: string) => {
    const api = terminalApiRef.current || terminalRef.current?.__openfocusRemoteTerminal || null;
    if (!api?.injectPromptToTerminal) {
      toast('Terminal unavailable');
      return;
    }
    try {
      const ok = await api.injectPromptToTerminal(text, { bracketedPaste: true, submit: false, focus: true });
      toast(ok ? 'Sent to terminal' : 'Terminal unavailable');
    } catch (err) {
      toast(`Send failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, []);

  const handleFileContextMenu = useCallback(
    (event: React.MouseEvent<HTMLElement>, entry: FileEntry) => {
      const relPath = String(entry.rel_path || '') || '.';
      showContextMenu(event, [
        {
          label: 'Send Path to Terminal',
          action: () => sendToTerminal(shellQuotePath(relPath)),
        },
      ]);
    },
    [sendToTerminal, showContextMenu],
  );

  const handlePreviewContextMenu = useCallback(
    (event: React.MouseEvent<HTMLElement>, options?: { allowSelection?: boolean }) => {
      const items: AgentContextMenuItem[] = [];
      const reference = options?.allowSelection === false ? '' : selectedPreviewReference();
      const fallbackReference = preview.path ? formatAgentFileReference(preview.path) : '';
      const payload = reference || fallbackReference;
      if (payload) items.push({ label: 'Send File Reference to Terminal', action: () => sendToTerminal(payload) });
      if (items.length) showContextMenu(event, items);
    },
    [preview.path, selectedPreviewReference, sendToTerminal, showContextMenu],
  );

  useEffect(() => {
    if (!contextMenu) return;
    const close = () => setContextMenu(null);
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    document.addEventListener('click', close);
    document.addEventListener('keydown', closeOnEscape);
    window.addEventListener('blur', close);
    window.addEventListener('resize', close);
    window.addEventListener('scroll', close, true);
    window.addEventListener('openfocus:agent-space-layout-changed', close);
    return () => {
      document.removeEventListener('click', close);
      document.removeEventListener('keydown', closeOnEscape);
      window.removeEventListener('blur', close);
      window.removeEventListener('resize', close);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('openfocus:agent-space-layout-changed', close);
    };
  }, [contextMenu]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data as TerminalLinkOpenMessage | null;
      if (!data || data.type !== 'openfocus:terminal-link-open') return;

      const href = cleanString(data.href);
      if (href && isHttpUrl(href)) {
        window.open(href, '_blank', 'noopener,noreferrer');
        return;
      }

      const fileReference = fileReferenceFromTerminalMessage(data, config.rootPath);
      if (!fileReference) {
        toast('File not found in workspace');
        return;
      }
      void openPreview(fileReference.relPath, guessNameFromPath(fileReference.relPath), { line: fileReference.line, column: fileReference.column });
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [config.rootPath, openPreview]);

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
      const api = window.OpenFocusRemoteTerminal.mount(el, {
        spaceId: config.spaceId,
        taskPublicId: config.taskPublicId,
        agentPrefix: config.agentPrefix,
        startAgentCommand: config.startAgentCommand || '',
      }) || el.__openfocusRemoteTerminal || null;
      terminalApiRef.current = api;
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error(err);
      window.alert(`Terminal initialization failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, [config.agentPrefix, config.spaceId, config.startAgentCommand, config.taskPublicId]);

  useEffect(() => {
    const copyButton = document.getElementById('space-copy-task');
    const cleanupButton = document.getElementById('space-release');
    const copyTaskId = async () => {
      try {
        await navigator.clipboard.writeText(config.taskPublicId);
        toast('Copied');
      } catch (_) {
        toast('Copy failed');
      }
    };
    const releaseSpace = async () => {
      if (!window.confirm('Release this AgentSpace? This only deletes OpenFocus records and will not delete local files.')) return;
      try {
        await releaseTaskAgentSpace(config.taskPublicId);
        toast('Released');
        window.location.href = `/goals?task=${encodeURIComponent(config.taskPublicId)}`;
      } catch (err) {
        toast('Release failed');
        window.alert(`Release failed: ${err instanceof Error ? err.message : String(err)}`);
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
              <FileTree spaceId={config.spaceId} onOpenFile={openPreview} onFileContextMenu={handleFileContextMenu} />
            </div>
          </div>
        </div>

        <div className="agent-space-splitter" data-split="left" title="Drag to resize FILES / PREVIEW" onMouseDown={(event) => startDrag('left', event)} onTouchStart={(event) => startDrag('left', event)} onDoubleClick={() => {
          const root = splitRef.current;
          if (!root) return;
          root.style.setProperty('--files-w', '340px');
          root.style.setProperty('--term-w', '420px');
          saveLayoutState(config.spaceId, { filesW: 340, termW: 420, ts: Date.now() });
        }} />

        <div className="panel" style={{ height: '100%', padding: 0 }}>
          <div style={{ height: '100%', minHeight: 0, display: 'flex', flexDirection: 'column' }}>
            <div className="pad" style={{ padding: 14, flex: '0 0 auto' }} onContextMenu={(event) => handlePreviewContextMenu(event, { allowSelection: false })}>
              <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10 }}>
                <div className="muted" style={{ fontSize: 12 }}>{preview.name || '—'}</div>
              </div>
            </div>
            <div className="divider" />
            <div ref={previewScrollRef} className="col-scroll pad" style={{ flex: '1 1 auto', minHeight: 0, height: 'auto', padding: 12, overflow: preview.content ? 'hidden' : 'auto' }} onContextMenu={(event) => handlePreviewContextMenu(event, { allowSelection: true })}>
              <div ref={previewContentRef} className={preview.path ? 'agent-preview-content' : 'muted'}>
                {preview.loading ? <><span className="spin" /> <span className="muted">Loading…</span></> : null}
                {preview.error ? preview.error : null}
                {!preview.loading && !preview.error && preview.imageUrl ? <img src={preview.imageUrl} style={{ maxWidth: '100%', height: 'auto' }} /> : null}
                {!preview.loading && !preview.error && preview.content ? <CodeMirrorPreview content={preview.content} name={preview.name} onScroll={savePreviewScroll} onSelectionChange={updatePreviewSelection} targetLine={preview.targetLine} targetColumn={preview.targetColumn} targetNonce={preview.targetNonce} /> : null}
                {!preview.path ? 'Select a file to preview (code / Markdown / image).' : null}
              </div>
            </div>
          </div>
        </div>

        <div className="agent-space-splitter" data-split="right" title="Drag to resize PREVIEW / TERMINAL" onMouseDown={(event) => startDrag('right', event)} onTouchStart={(event) => startDrag('right', event)} onDoubleClick={() => {
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
      {contextMenu ? (
        <div
          role="menu"
          style={{
            position: 'fixed',
            left: contextMenu.x,
            top: contextMenu.y,
            zIndex: 10000,
            minWidth: 210,
            padding: 6,
            border: '1px solid rgba(0, 229, 255, 0.28)',
            borderRadius: 8,
            background: 'rgba(5, 10, 18, 0.97)',
            boxShadow: '0 18px 40px rgba(0, 0, 0, 0.42)',
          }}
          onClick={(event) => event.stopPropagation()}
          onContextMenu={(event) => event.preventDefault()}
        >
          {contextMenu.items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              className="btn-ghost"
              style={{ width: '100%', display: 'block', textAlign: 'left', margin: 0 }}
              onClick={() => {
                setContextMenu(null);
                void item.action();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}

const mount = document.getElementById('agent-space-react-root');
if (mount) {
  const config = JSON.parse(mount.getAttribute('data-config') || '{}') as AgentSpaceConfig;
  createRoot(mount).render(<AgentSpaceApp config={config} />);
}

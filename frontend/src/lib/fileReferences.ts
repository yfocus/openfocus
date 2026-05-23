/* SPDX-License-Identifier: Apache-2.0 */

export type TerminalLinkOpenMessage = {
  type?: unknown;
  href?: unknown;
  text?: unknown;
  path?: unknown;
  line?: unknown;
  column?: unknown;
  terminalId?: unknown;
};

export type ParsedFileReference = {
  path: string;
  line?: number;
  column?: number;
};

export type WorkspaceFileReference = {
  relPath: string;
  line?: number;
  column?: number;
};

export function positiveInt(value: unknown): number | undefined {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 1) return undefined;
  return Math.floor(n);
}

export function cleanString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

export function formatAgentFileReference(relPath: string, line?: number): string {
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

export function isHttpUrl(value: string): boolean {
  return /^https?:\/\//i.test(String(value || '').trim());
}

export function extractFileReferenceCandidates(raw: string): string[] {
  const value = String(raw || '');
  const candidates: string[] = [];
  const pattern = /@?file:\/\/[^\s<>"'`\)\]}]+|@?(?:\.{1,2}\/|\/|[A-Za-z0-9_.@+-]+\/)[^\s<>"'`\)\]}]+|@?[A-Za-z0-9_.@+-]+\.[A-Za-z0-9_+-]{1,16}(?::\d+){0,2}(?:#L\d+(?:C\d+)?)?/gi;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(value))) {
    const candidate = stripFileReference(match[0] || '');
    if (candidate) candidates.push(candidate);
  }
  return Array.from(new Set(candidates));
}

export function parseSingleFileReference(raw: string): ParsedFileReference | null {
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

export function parseTerminalFileReference(raw: string): ParsedFileReference | null {
  const directCandidate = stripFileReference(raw);
  const direct = /\s/.test(directCandidate) ? null : parseSingleFileReference(directCandidate);
  if (direct) return direct;
  for (const candidate of extractFileReferenceCandidates(raw)) {
    const parsed = parseSingleFileReference(candidate);
    if (parsed) return parsed;
  }
  return null;
}

export function normalizeAbsolutePath(value: string): string | null {
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
    if (part.includes(String.fromCharCode(0))) return null;
    parts.push(part);
  }
  return `/${parts.join('/')}`;
}

export function normalizeRelativePath(value: string): string | null {
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
    if (part.includes(String.fromCharCode(0))) return null;
    parts.push(part);
  }
  return parts.length ? parts.join('/') : null;
}

export function workspaceRelativePath(candidatePath: string, rootPath: string): string | null {
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

export function fileReferenceFromTerminalMessage(
  data: TerminalLinkOpenMessage,
  rootPath: string,
): WorkspaceFileReference | null {
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

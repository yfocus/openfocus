/* SPDX-License-Identifier: Apache-2.0 */

import { normalizeAgentSpaceFilePath } from './agentSpaceFileReveal.js';

export type AgentSpaceOpenFileEntry = {
  path: string;
  name: string;
  size?: number;
  mtime?: number;
};

export type AgentSpaceOpenFileMatch = {
  file: AgentSpaceOpenFileEntry;
  score: number;
};

export type OpenFilePreviewOpener = (
  relPath: string,
  name: string,
  target?: { source?: string },
) => void | Promise<void>;

type RawOpenFileEntry = {
  path?: unknown;
  name?: unknown;
  size?: unknown;
  mtime?: unknown;
};

function positiveNumber(value: unknown): number | undefined {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined;
}

export function openFileNameFromPath(path: string): string {
  const value = normalizeAgentSpaceFilePath(path);
  if (!value) return 'Untitled';
  const idx = value.lastIndexOf('/');
  return idx >= 0 ? value.slice(idx + 1) || value : value;
}

export function normalizeOpenFileEntries(rawFiles?: unknown, rawPaths?: unknown): AgentSpaceOpenFileEntry[] {
  const out: AgentSpaceOpenFileEntry[] = [];
  const seen = new Set<string>();

  const add = (entry: RawOpenFileEntry) => {
    const path = normalizeAgentSpaceFilePath(String(entry.path || ''));
    if (!path || seen.has(path)) return;
    seen.add(path);
    const name = String(entry.name || '').trim() || openFileNameFromPath(path);
    const size = positiveNumber(entry.size);
    const mtime = positiveNumber(entry.mtime);
    out.push({
      path,
      name,
      ...(size === undefined ? {} : { size }),
      ...(mtime === undefined ? {} : { mtime }),
    });
  };

  if (Array.isArray(rawFiles)) {
    for (const file of rawFiles) {
      if (!file || typeof file !== 'object') continue;
      add(file as RawOpenFileEntry);
    }
  }

  if (Array.isArray(rawPaths)) {
    for (const path of rawPaths) add({ path });
  }

  return out;
}

function queryTokens(query: string): string[] {
  return String(query || '')
    .trim()
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
}

function charBoundaryBonus(candidate: string, index: number): number {
  if (index <= 0) return 30;
  const prev = candidate[index - 1] || '';
  return prev === '/' || prev === '-' || prev === '_' || prev === '.' ? 18 : 0;
}

function fuzzyCandidateScore(candidate: string, token: string): number | null {
  const value = candidate.toLowerCase();
  if (!value || !token) return null;
  const directIndex = value.indexOf(token);
  if (directIndex >= 0) {
    return 1000 + charBoundaryBonus(value, directIndex) - directIndex * 2 - Math.max(0, value.length - token.length);
  }

  let searchFrom = 0;
  let firstIndex = -1;
  let previousIndex = -1;
  let gapPenalty = 0;
  let boundaryBonus = 0;
  for (const char of token) {
    const index = value.indexOf(char, searchFrom);
    if (index < 0) return null;
    if (firstIndex < 0) {
      firstIndex = index;
      boundaryBonus = charBoundaryBonus(value, index);
    }
    if (previousIndex >= 0) gapPenalty += Math.max(0, index - previousIndex - 1);
    previousIndex = index;
    searchFrom = index + 1;
  }

  return 420 + boundaryBonus - firstIndex * 3 - gapPenalty * 5 - Math.max(0, value.length - token.length);
}

function fileTokenScore(file: AgentSpaceOpenFileEntry, token: string): number | null {
  const pathScore = fuzzyCandidateScore(file.path, token);
  const nameScore = fuzzyCandidateScore(file.name, token);
  if (pathScore === null && nameScore === null) return null;
  const best = Math.max(pathScore ?? Number.NEGATIVE_INFINITY, nameScore ?? Number.NEGATIVE_INFINITY);
  return best + (nameScore !== null && nameScore >= (pathScore ?? Number.NEGATIVE_INFINITY) ? 60 : 0);
}

export function filterOpenFileEntries(
  files: AgentSpaceOpenFileEntry[],
  query: string,
  limit = 100,
): AgentSpaceOpenFileMatch[] {
  const safeLimit = Math.max(1, Math.floor(Number(limit || 100)));
  const normalizedFiles = normalizeOpenFileEntries(files);
  const tokens = queryTokens(query);
  if (!tokens.length) {
    return normalizedFiles.slice(0, safeLimit).map((file) => ({ file, score: 0 }));
  }

  const matches: AgentSpaceOpenFileMatch[] = [];
  for (const file of normalizedFiles) {
    let score = 0;
    let matched = true;
    for (const token of tokens) {
      const tokenScore = fileTokenScore(file, token);
      if (tokenScore === null) {
        matched = false;
        break;
      }
      score += tokenScore;
    }
    if (matched) matches.push({ file, score });
  }

  return matches
    .sort((left, right) => {
      if (right.score !== left.score) return right.score - left.score;
      if (left.file.path.length !== right.file.path.length) return left.file.path.length - right.file.path.length;
      return left.file.path.localeCompare(right.file.path);
    })
    .slice(0, safeLimit);
}

export function openFileOverlayStatusText(options: {
  error?: string;
  loading?: boolean;
  filesLoaded?: boolean;
  resultCount?: number;
  total?: number;
  truncated?: boolean;
  cacheHit?: boolean;
}): string {
  const error = String(options.error || '');
  if (error) return error;
  if (options.loading) return 'Loading files...';
  const total = Math.max(0, Math.floor(Number(options.total || 0)));
  if (options.filesLoaded && Number(options.resultCount || 0) <= 0) return total > 0 ? 'No matching files' : 'No files';
  if (!options.filesLoaded || total <= 0) return '';
  const cacheText = options.cacheHit ? 'cached' : 'fresh';
  return `${total} ${total === 1 ? 'file' : 'files'} (${cacheText})${options.truncated ? ', partial' : ''}`;
}

export async function openAgentSpaceFileMatch(
  match: AgentSpaceOpenFileMatch | null | undefined,
  openPreview: OpenFilePreviewOpener,
): Promise<boolean> {
  if (!match?.file.path) return false;
  await openPreview(match.file.path, match.file.name || openFileNameFromPath(match.file.path), { source: 'open_file' });
  return true;
}

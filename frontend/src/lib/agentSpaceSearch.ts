/* SPDX-License-Identifier: Apache-2.0 */

import {
  groupCodeSearchResults,
  type CodeNavigationBackend,
  type CodeSearchResponse,
  type CodeSearchResult,
  type CodeSearchResultGroup,
} from '../api/codeNavigation.js';

export type SearchPreviewOpener = (relPath: string, name: string, target?: { line?: number; column?: number; source?: string }) => void | Promise<void>;

export function searchResultFileName(path: string): string {
  const value = String(path || '').trim();
  if (!value) return 'Untitled';
  const idx = value.lastIndexOf('/');
  return idx >= 0 ? value.slice(idx + 1) || value : value;
}

export function codeSearchGroups(response: Pick<CodeSearchResponse, 'groups' | 'results'> | null | undefined): CodeSearchResultGroup[] {
  if (!response) return [];
  if (Array.isArray(response.groups) && response.groups.length > 0) return response.groups;
  return groupCodeSearchResults(Array.isArray(response.results) ? response.results : []);
}

export function flattenCodeSearchGroups(groups: CodeSearchResultGroup[]): CodeSearchResult[] {
  const flattened: CodeSearchResult[] = [];
  for (const group of groups) {
    for (const result of group.results || []) flattened.push(result);
  }
  return flattened;
}

export function moveSearchSelection(currentIndex: number, total: number, delta: number): number {
  if (total <= 0) return -1;
  if (!Number.isFinite(currentIndex) || currentIndex < 0) return delta < 0 ? total - 1 : 0;
  const next = currentIndex + delta;
  if (next < 0) return total - 1;
  if (next >= total) return 0;
  return next;
}

export function codeSearchResultPrimaryLabel(result: CodeSearchResult): string {
  const name = String(result.name || '').trim();
  if (name) return name;
  if (result.kind === 'file') return searchResultFileName(result.path);
  const preview = String(result.preview || '').trim().replace(/\s+/g, ' ');
  if (preview) return preview;
  return searchResultFileName(result.path);
}

export function codeSearchResultMetaLabel(result: CodeSearchResult): string {
  const path = String(result.path || '');
  const line = positiveInt(result.line);
  const column = positiveInt(result.column);
  const location = line ? `${path}:${line}${column ? `:${column}` : ''}` : path;
  const kind = String(result.kind || 'result');
  const container = String(result.container || '').trim();
  return container ? `${kind} in ${container} - ${location}` : `${kind} - ${location}`;
}

export function codeSearchBackendLabel(backend: CodeNavigationBackend | string | null | undefined): string {
  switch (backend) {
    case 'rg':
      return 'Text';
    case 'lsp':
      return 'Semantic';
    case 'text_fallback':
      return 'Text fallback';
    case 'symbol_fallback':
      return 'Symbol fallback';
    case 'definition_fallback':
      return 'Definition fallback';
    case 'reference_fallback':
      return 'Reference fallback';
    default:
      return '';
  }
}

export function shouldRunCodeSearchQuery(query: string | null | undefined): boolean {
  return String(query || '').trim().length > 0;
}

export function codeSearchOverlayStatusText(options: {
  error?: string;
  status?: string;
  completed?: boolean;
  loading?: boolean;
  resultCount?: number;
}): string {
  const error = String(options.error || '');
  if (error) return error;
  const status = String(options.status || '');
  if (status) return status;
  if (options.completed && !options.loading && Number(options.resultCount || 0) <= 0) return 'No results';
  return '';
}

export async function openCodeSearchResult(
  result: CodeSearchResult | null | undefined,
  openPreview: SearchPreviewOpener,
  source = 'search_everywhere',
): Promise<boolean> {
  if (!result?.path) return false;
  const line = positiveInt(result.line);
  const column = positiveInt(result.column);
  await openPreview(result.path, searchResultFileName(result.path), {
    line: line || undefined,
    column: column || undefined,
    source,
  });
  return true;
}

export function isCompanionOfflineSearchError(error: unknown): boolean {
  const text = error instanceof Error ? error.message : String(error || '');
  const lower = text.toLowerCase();
  if (!lower) return false;
  return lower.includes('companion') && (lower.includes('offline') || lower.includes('unavailable') || lower.includes('not available'));
}

function positiveInt(value: unknown): number {
  const parsed = Math.floor(Number(value || 0));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

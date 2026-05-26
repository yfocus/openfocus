/* SPDX-License-Identifier: Apache-2.0 */

import type {
  CodeNavigationBackend,
  CodeReferenceResult,
} from '../api/codeNavigation.js';
import { searchResultFileName } from './agentSpaceSearch.js';

export type CodeReferenceResultGroup = {
  path: string;
  results: CodeReferenceResult[];
};

export type ReferencePreviewOpener = (relPath: string, name: string, target?: { line?: number; column?: number; source?: string }) => void | Promise<void>;

export function groupCodeReferenceResults(results: CodeReferenceResult[]): CodeReferenceResultGroup[] {
  const groups: CodeReferenceResultGroup[] = [];
  const byPath = new Map<string, CodeReferenceResultGroup>();
  for (const result of results || []) {
    const path = String(result.path || '');
    if (!path) continue;
    let group = byPath.get(path);
    if (!group) {
      group = { path, results: [] };
      byPath.set(path, group);
      groups.push(group);
    }
    group.results.push(result);
  }
  return groups;
}

export function flattenCodeReferenceGroups(groups: CodeReferenceResultGroup[]): CodeReferenceResult[] {
  const flattened: CodeReferenceResult[] = [];
  for (const group of groups || []) {
    for (const result of group.results || []) flattened.push(result);
  }
  return flattened;
}

export function codeReferenceBackendLabel(backend: CodeNavigationBackend | string | null | undefined): string {
  switch (backend) {
    case 'lsp':
      return 'Semantic references';
    case 'reference_fallback':
      return 'Text matches';
    case 'text_fallback':
      return 'Text fallback';
    case 'rg':
      return 'Text matches';
    default:
      return backend ? String(backend) : '';
  }
}

export function codeReferenceDrawerTitle(backend: CodeNavigationBackend | string | null | undefined): string {
  return backend === 'lsp' ? 'References' : 'Possible references';
}

export function codeReferenceNoResultsMessage(backend: CodeNavigationBackend | string | null | undefined): string {
  return backend === 'lsp' ? 'No references found' : 'No text matches found';
}

export function codeReferenceResultPreview(result: CodeReferenceResult): string {
  const preview = String(result.preview || '').trim().replace(/\s+/g, ' ');
  return preview || searchResultFileName(result.path);
}

export function codeReferenceResultLocationLabel(result: CodeReferenceResult): string {
  const line = positiveInt(result.line);
  const column = positiveInt(result.column);
  if (!line) return '';
  return `L${line}${column ? `:${column}` : ''}`;
}

export async function openCodeReferenceResult(result: CodeReferenceResult | null | undefined, openPreview: ReferencePreviewOpener): Promise<boolean> {
  if (!result?.path) return false;
  const line = positiveInt(result.line);
  const column = positiveInt(result.column);
  await openPreview(result.path, searchResultFileName(result.path), {
    line: line || undefined,
    column: column || undefined,
    source: 'find_usages',
  });
  return true;
}

function positiveInt(value: unknown): number {
  const parsed = Math.floor(Number(value || 0));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

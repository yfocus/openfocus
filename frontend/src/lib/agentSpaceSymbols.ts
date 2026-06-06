/* SPDX-License-Identifier: Apache-2.0 */

import type { CodeSymbolResult } from '../api/codeNavigation.js';
import { searchResultFileName } from './agentSpaceSearch.js';

export type WorkspaceSymbolPreviewOpener = (
  relPath: string,
  name: string,
  target?: { line?: number; column?: number; source?: string },
) => void | Promise<void>;

type SymbolLabelResult = Pick<CodeSymbolResult, 'path' | 'name'>;
type SymbolMetaResult = Pick<CodeSymbolResult, 'path' | 'line' | 'column' | 'kind' | 'container'>;

export function symbolResultPrimaryLabel(result: SymbolLabelResult): string {
  const name = String(result.name || '').trim();
  if (name) return name;
  return searchResultFileName(result.path);
}

export function symbolResultPreviewName(result: Pick<CodeSymbolResult, 'path'>): string {
  return searchResultFileName(result.path);
}

export function symbolResultMetaLabel(result: SymbolMetaResult): string {
  const path = String(result.path || '');
  const line = positiveInt(result.line);
  const column = positiveInt(result.column);
  const location = line ? `${path}:${line}${column ? `:${column}` : ''}` : path;
  const kind = String(result.kind || 'symbol');
  const container = String(result.container || '').trim();
  return container ? `${kind} in ${container} - ${location}` : `${kind} - ${location}`;
}

export function workspaceSymbolOverlayStatusText(options: {
  error?: string;
  status?: string;
  completed?: boolean;
  loading?: boolean;
  resultCount?: number;
}): string {
  const error = String(options.error || '');
  if (error) return error;
  if (options.loading) return 'Searching symbols...';
  const status = String(options.status || '');
  if (status) return status;
  if (options.completed && Number(options.resultCount || 0) <= 0) return 'No symbols';
  return '';
}

export async function openWorkspaceSymbolResult(
  result: CodeSymbolResult | null | undefined,
  openPreview: WorkspaceSymbolPreviewOpener,
): Promise<boolean> {
  if (!result?.path) return false;
  const line = positiveInt(result.line);
  const column = positiveInt(result.column);
  await openPreview(result.path, symbolResultPreviewName(result), {
    line: line || undefined,
    column: column || undefined,
    source: 'go_to_symbol',
  });
  return true;
}

function positiveInt(value: unknown): number {
  const parsed = Math.floor(Number(value || 0));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

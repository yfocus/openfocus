/* SPDX-License-Identifier: Apache-2.0 */

import type { CodeSymbolResult } from '../api/codeNavigation.js';
import { searchResultFileName } from './agentSpaceSearch.js';

type DefinitionLabelResult = Pick<CodeSymbolResult, 'path' | 'name'>;
type DefinitionMetaResult = Pick<CodeSymbolResult, 'path' | 'line' | 'column' | 'kind' | 'container'>;

export function definitionResultPrimaryLabel(result: DefinitionLabelResult): string {
  const name = String(result.name || '').trim();
  if (name) return name;
  return searchResultFileName(result.path);
}

export function definitionResultPreviewName(result: Pick<CodeSymbolResult, 'path'>): string {
  return searchResultFileName(result.path);
}

export function definitionResultMetaLabel(result: DefinitionMetaResult): string {
  const path = String(result.path || '');
  const line = positiveInt(result.line);
  const column = positiveInt(result.column);
  const location = line ? `${path}:${line}${column ? `:${column}` : ''}` : path;
  const kind = String(result.kind || 'definition');
  const container = String(result.container || '').trim();
  return container ? `${kind} in ${container} - ${location}` : `${kind} - ${location}`;
}

function positiveInt(value: unknown): number {
  const parsed = Math.floor(Number(value || 0));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

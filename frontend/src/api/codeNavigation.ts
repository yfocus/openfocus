/* SPDX-License-Identifier: Apache-2.0 */

import { apiJson, jsonOptions } from './client.js';

export type CodeSearchKind = 'all' | 'file' | 'text' | 'symbol';

export type CodeNavigationBackend =
  | 'rg'
  | 'lsp'
  | 'text_fallback'
  | 'symbol_fallback'
  | 'definition_fallback'
  | 'reference_fallback';

export type CodeSearchResultKind = 'file' | 'text' | 'class' | 'function' | 'variable' | 'method';

export type CodeSymbolResultKind = 'class' | 'function' | 'variable' | 'method';

export type CodeReferenceResultKind = 'reference';

export type CodeLocation = {
  path: string;
  line: number;
  column: number;
  end_line?: number;
  end_column?: number;
};

export type CodeNavigationResponseMeta = {
  ok: boolean;
  backend: CodeNavigationBackend;
  truncated?: boolean;
  error?: string;
};

export type CodeSearchResult = CodeLocation & {
  kind: CodeSearchResultKind;
  preview?: string;
  score?: number;
  backend?: CodeNavigationBackend;
  name?: string;
  container?: string;
};

export type CodeSymbolResult = CodeLocation & {
  kind: CodeSymbolResultKind;
  name: string;
  container?: string;
  preview?: string;
  score?: number;
  backend?: CodeNavigationBackend;
};

export type CodeReferenceResult = CodeLocation & {
  kind: CodeReferenceResultKind;
  preview?: string;
  score?: number;
  backend?: CodeNavigationBackend;
};

export type CodeSearchResponse = CodeNavigationResponseMeta & {
  query: string;
  kind: CodeSearchKind;
  results: CodeSearchResult[];
  groups: CodeSearchResultGroup[];
};

export type CodeSearchResultGroup = {
  path: string;
  results: CodeSearchResult[];
};

export type CodeSymbolsResponse = CodeNavigationResponseMeta & {
  query: string;
  results: CodeSymbolResult[];
};

export type CodeDefinitionResponse = CodeNavigationResponseMeta & {
  symbol: string;
  results: CodeSymbolResult[];
};

export type CodeReferencesResponse = CodeNavigationResponseMeta & {
  symbol: string;
  results: CodeReferenceResult[];
};

export type CodeNavigationStatus = CodeNavigationResponseMeta & {
  ripgrep_available: boolean;
  lsp_available: boolean;
  active_language_servers: string[];
  fallback_mode: boolean;
};

export type CodeSearchOptions = {
  q?: string;
  kind?: CodeSearchKind;
  include?: string;
  exclude?: string;
  caseSensitive?: boolean;
  regex?: boolean;
  limit?: number;
};

export type CodeSymbolsOptions = {
  q?: string;
  limit?: number;
};

export type CodeNavigationLocationRequest = {
  path: string;
  line: number;
  column: number;
  symbol: string;
};

export function searchCode(spaceId: number, options: CodeSearchOptions = {}): Promise<CodeSearchResponse> {
  const params = queryParams({
    q: options.q,
    kind: options.kind,
    include: options.include,
    exclude: options.exclude,
    case_sensitive: options.caseSensitive,
    regex: options.regex,
    limit: options.limit,
  });
  return apiJson<CodeSearchResponse>(`/api/agent_spaces/${spaceId}/code/search${params}`);
}

export function getCodeSymbols(spaceId: number, options: CodeSymbolsOptions = {}): Promise<CodeSymbolsResponse> {
  const params = queryParams({
    q: options.q,
    limit: options.limit,
  });
  return apiJson<CodeSymbolsResponse>(`/api/agent_spaces/${spaceId}/code/symbols${params}`);
}

export function groupCodeSearchResults(results: CodeSearchResult[]): CodeSearchResultGroup[] {
  const groups: CodeSearchResultGroup[] = [];
  const byPath = new Map<string, CodeSearchResultGroup>();
  for (const result of results) {
    let group = byPath.get(result.path);
    if (!group) {
      group = { path: result.path, results: [] };
      byPath.set(result.path, group);
      groups.push(group);
    }
    group.results.push(result);
  }
  return groups;
}

export function findCodeDefinition(
  spaceId: number,
  request: CodeNavigationLocationRequest,
): Promise<CodeDefinitionResponse> {
  return apiJson<CodeDefinitionResponse>(
    `/api/agent_spaces/${spaceId}/code/definition`,
    jsonOptions(request, { method: 'POST' }),
  );
}

export function findCodeReferences(
  spaceId: number,
  request: CodeNavigationLocationRequest,
): Promise<CodeReferencesResponse> {
  return apiJson<CodeReferencesResponse>(
    `/api/agent_spaces/${spaceId}/code/references`,
    jsonOptions(request, { method: 'POST' }),
  );
}

export function getCodeNavigationStatus(spaceId: number): Promise<CodeNavigationStatus> {
  return apiJson<CodeNavigationStatus>(`/api/agent_spaces/${spaceId}/code/status`);
}

function queryParams(values: Record<string, string | number | boolean | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined || value === '' || value === false) {
      continue;
    }
    params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : '';
}

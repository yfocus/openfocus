/* SPDX-License-Identifier: Apache-2.0 */

import { apiJson } from './client';
import type { FileEntry } from '../types/openfocus';

export type AgentSpaceFilePathEntry = {
  path: string;
  name: string;
  size?: number;
  mtime?: number;
};

export type AgentSpaceFilePathCache = {
  hit?: boolean;
  refresh?: boolean;
  [key: string]: unknown;
};

export type AgentSpaceFilePathsResponse = {
  ok?: boolean;
  files?: AgentSpaceFilePathEntry[];
  paths?: string[];
  total?: number;
  truncated?: boolean;
  cache?: AgentSpaceFilePathCache;
};

export function listFiles(spaceId: number, path: string): Promise<{ entries?: FileEntry[] }> {
  return apiJson<{ entries?: FileEntry[] }>(`/api/agent_spaces/${spaceId}/files/list?path=${encodeURIComponent(path || '')}`);
}

export function listFilePaths(spaceId: number, refresh = false): Promise<AgentSpaceFilePathsResponse> {
  const query = refresh ? '?refresh=true' : '';
  return apiJson<AgentSpaceFilePathsResponse>(`/api/agent_spaces/${spaceId}/files/paths${query}`);
}

export function readFile(spaceId: number, path: string): Promise<{ content?: string }> {
  return apiJson<{ content?: string }>(`/api/agent_spaces/${spaceId}/files/read?path=${encodeURIComponent(path || '')}`);
}

export function rawFileUrl(spaceId: number, path: string): string {
  return `/api/agent_spaces/${spaceId}/files/raw?path=${encodeURIComponent(path || '')}`;
}

export function releaseTaskAgentSpace(taskPublicId: string): Promise<Record<string, unknown>> {
  return apiJson<Record<string, unknown>>(`/api/tasks/${encodeURIComponent(taskPublicId)}/agent_space`, { method: 'DELETE' });
}

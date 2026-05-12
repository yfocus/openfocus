/* SPDX-License-Identifier: Apache-2.0 */

import { apiJson, jsonOptions } from './client';
import type { IdPayload, InspirationBusyState } from '../types/openfocus';

export function getInspiration(spaceId: number): Promise<InspirationBusyState> {
  return apiJson<InspirationBusyState>(`/api/inspirations/${spaceId}`);
}

export function createInspiration(payload: { title: string; initial_message: string; mode: string }): Promise<IdPayload> {
  return apiJson<IdPayload>('/api/inspirations', { method: 'POST', ...jsonOptions(payload) });
}

export function postMessage(spaceId: number, content: string): Promise<Record<string, unknown>> {
  return apiJson(`/api/inspirations/${spaceId}/messages`, { method: 'POST', keepalive: true, ...jsonOptions({ content }) });
}

export function terminalApiBase(spaceId: number): string {
  return `/api/inspirations/${spaceId}/terminals`;
}

export function syncResourcesUrl(spaceId: number): string {
  return `/api/inspirations/${spaceId}/resources/sync`;
}

export function syncResources(spaceId: number): Promise<Record<string, unknown>> {
  return apiJson(syncResourcesUrl(spaceId), { method: 'POST' });
}

export function generateDraftFromResource(spaceId: number, resourceId: number): Promise<Record<string, unknown>> {
  return apiJson(`/api/inspirations/${spaceId}/drafts/generate_from_resource`, { method: 'POST', ...jsonOptions({ resource_id: resourceId }) });
}

export function updateResource(spaceId: number, resourceId: number, payload: { name: string; url_content?: string; text_content?: string }): Promise<Record<string, unknown>> {
  return apiJson(`/api/inspirations/${spaceId}/resources/${resourceId}`, { method: 'PATCH', ...jsonOptions(payload) });
}

export function replaceResource(spaceId: number, resourceId: number, body: FormData): Promise<Record<string, unknown>> {
  return apiJson(`/api/inspirations/${spaceId}/resources/${resourceId}/replace`, { method: 'POST', body });
}

export function createResource(spaceId: number, body: FormData): Promise<Record<string, unknown>> {
  return apiJson(`/api/inspirations/${spaceId}/resources`, { method: 'POST', body });
}

export function closeSpace(spaceId: number): Promise<Record<string, unknown>> {
  return apiJson(`/api/inspirations/${spaceId}/close`, { method: 'POST' });
}

export function reopenSpace(spaceId: number): Promise<Record<string, unknown>> {
  return apiJson(`/api/inspirations/${spaceId}/reopen`, { method: 'POST' });
}

export function deleteSpace(spaceId: number): Promise<Record<string, unknown>> {
  return apiJson(`/api/inspirations/${spaceId}`, { method: 'DELETE' });
}

export function forkSpace(spaceId: number, payload: { title: string; include_all_resources: boolean }): Promise<IdPayload> {
  return apiJson<IdPayload>(`/api/inspirations/${spaceId}/fork`, { method: 'POST', ...jsonOptions(payload) });
}

export function deleteResource(spaceId: number, resourceId: number): Promise<Record<string, unknown>> {
  return apiJson(`/api/inspirations/${spaceId}/resources/${resourceId}`, { method: 'DELETE' });
}

export function publishDraft(spaceId: number, payload: { draft_id: number; due_date: string }): Promise<Record<string, unknown>> {
  return apiJson(`/api/inspirations/${spaceId}/publish`, { method: 'POST', ...jsonOptions(payload) });
}

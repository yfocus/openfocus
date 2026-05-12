/* SPDX-License-Identifier: Apache-2.0 */

export async function apiJson<T = Record<string, unknown>>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options || {});
  if (!response.ok) {
    const text = await response.text().catch(() => 'request failed');
    throw new Error(text || `HTTP ${response.status}`);
  }
  const contentType = String(response.headers.get('content-type') || '');
  return contentType.includes('application/json') ? ((await response.json()) as T) : ({} as T);
}

export function jsonOptions(payload: unknown, init?: RequestInit): RequestInit {
  return {
    ...(init || {}),
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    body: JSON.stringify(payload),
  };
}

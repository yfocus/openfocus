/* SPDX-License-Identifier: Apache-2.0 */

export type AgentSpaceFileRevealPlan = {
  targetPath: string;
  ancestorPaths: string[];
};

export function normalizeAgentSpaceFilePath(path: string): string {
  const raw = String(path || '').trim().replace(/\\/g, '/');
  if (!raw) return '';

  const segments = raw
    .split('/')
    .filter((segment) => segment && segment !== '.');
  if (!segments.length || segments.includes('..')) return '';
  return segments.join('/');
}

export function agentSpaceFileAncestorPaths(path: string): string[] {
  const targetPath = normalizeAgentSpaceFilePath(path);
  if (!targetPath) return [];

  const segments = targetPath.split('/');
  const ancestors = [''];
  for (let index = 1; index < segments.length; index += 1) {
    ancestors.push(segments.slice(0, index).join('/'));
  }
  return ancestors;
}

export function createAgentSpaceFileRevealPlan(path: string): AgentSpaceFileRevealPlan | null {
  const targetPath = normalizeAgentSpaceFilePath(path);
  if (!targetPath) return null;
  return {
    targetPath,
    ancestorPaths: agentSpaceFileAncestorPaths(targetPath),
  };
}

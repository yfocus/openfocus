/* SPDX-License-Identifier: Apache-2.0 */

export type AgentSpaceSettings = {
  filesFontSize: number;
  previewFontSize: number;
  terminalFontSize: number;
  showFiles: boolean;
  showPreview: boolean;
  showTerminal: boolean;
};

export const AGENT_SPACE_SETTINGS_KEY = 'openfocus.agent_space.settings.v1';
export const AGENT_SPACE_SETTINGS_EVENT = 'openfocus:agent-space-settings-changed';
export const DEFAULT_AGENT_SPACE_SETTINGS: AgentSpaceSettings = {
  filesFontSize: 13,
  previewFontSize: 12,
  terminalFontSize: 13,
  showFiles: true,
  showPreview: true,
  showTerminal: true,
};

function clamp(value: number, minValue: number, maxValue: number): number {
  if (!Number.isFinite(value)) return minValue;
  if (value < minValue) return minValue;
  if (value > maxValue) return maxValue;
  return value;
}

function clampSetting(value: unknown, minValue: number, maxValue: number, fallback: number): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.round(clamp(n, minValue, maxValue));
}

export function normalizeAgentSpaceSettings(raw: Partial<AgentSpaceSettings> | null | undefined): AgentSpaceSettings {
  const src = raw && typeof raw === 'object' ? raw : {};
  return {
    filesFontSize: clampSetting(src.filesFontSize, 10, 24, DEFAULT_AGENT_SPACE_SETTINGS.filesFontSize),
    previewFontSize: clampSetting(src.previewFontSize, 10, 24, DEFAULT_AGENT_SPACE_SETTINGS.previewFontSize),
    terminalFontSize: clampSetting(src.terminalFontSize, 10, 24, DEFAULT_AGENT_SPACE_SETTINGS.terminalFontSize),
    showFiles: src.showFiles !== false,
    showPreview: src.showPreview !== false,
    showTerminal: src.showTerminal !== false,
  };
}

export function loadAgentSpaceSettings(): AgentSpaceSettings {
  try {
    const raw = localStorage.getItem(AGENT_SPACE_SETTINGS_KEY);
    return normalizeAgentSpaceSettings(raw ? JSON.parse(raw) as Partial<AgentSpaceSettings> : null);
  } catch (_) {
    return normalizeAgentSpaceSettings(null);
  }
}

export function saveAgentSpaceSettings(settings: Partial<AgentSpaceSettings>, source = 'agent-space'): AgentSpaceSettings {
  const next = normalizeAgentSpaceSettings(settings);
  try {
    localStorage.setItem(AGENT_SPACE_SETTINGS_KEY, JSON.stringify(next));
  } catch (_) {
    // ignore storage failures
  }
  try {
    window.dispatchEvent(new CustomEvent(AGENT_SPACE_SETTINGS_EVENT, { detail: { settings: next, source } }));
  } catch (_) {
    // ignore event failures
  }
  return next;
}

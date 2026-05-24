/* SPDX-License-Identifier: Apache-2.0 */

export type ShortcutPlatform = 'all' | 'mac' | 'other';

export type AgentSpaceShortcutCommandId =
  | 'search_everywhere'
  | 'find_in_files'
  | 'go_to_definition'
  | 'find_usages'
  | 'navigation_back'
  | 'navigation_forward'
  | 'focus_files'
  | 'focus_preview'
  | 'focus_terminal';

export type ShortcutBinding = {
  keys: string[];
  platform: ShortcutPlatform;
};

export type AgentSpaceShortcutCommand = {
  id: AgentSpaceShortcutCommandId;
  label: string;
  scope: string;
  defaults: ShortcutBinding[];
};

export type AgentSpaceShortcutSettings = {
  version: 1;
  bindings: Record<AgentSpaceShortcutCommandId, ShortcutBinding[]>;
};

export type ShortcutConflict = {
  commandId: AgentSpaceShortcutCommandId;
  label: string;
  binding: ShortcutBinding;
};

export type ShortcutValidationResult =
  | { ok: true; binding: ShortcutBinding }
  | { ok: false; reason: 'empty' | 'reserved_browser_shortcut' | 'plain_key_without_modifier' | 'conflict'; conflict?: ShortcutConflict };

export type ShortcutKeyEventLike = {
  key: string;
  code?: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  repeat?: boolean;
};

export type ShortcutRecordingAction =
  | { action: 'ignore' }
  | { action: 'cancel' }
  | { action: 'clear' }
  | { action: 'confirm'; binding: ShortcutBinding }
  | { action: 'capture'; binding: ShortcutBinding };

export const AGENT_SPACE_SHORTCUTS_KEY = 'openfocus.agent_space.shortcuts.v1';
export const AGENT_SPACE_SHORTCUTS_EVENT = 'openfocus:agent-space-shortcuts-changed';

export const AGENT_SPACE_SHORTCUT_COMMANDS: AgentSpaceShortcutCommand[] = [
  {
    id: 'search_everywhere',
    label: 'Search Everywhere',
    scope: 'global except active terminal input',
    defaults: [{ keys: ['Shift', 'Shift'], platform: 'all' }],
  },
  {
    id: 'find_in_files',
    label: 'Find in Files',
    scope: 'global except active terminal input',
    defaults: [
      { keys: ['Meta', 'Shift', 'F'], platform: 'mac' },
      { keys: ['Ctrl', 'Shift', 'F'], platform: 'other' },
    ],
  },
  {
    id: 'go_to_definition',
    label: 'Go to Definition',
    scope: 'PREVIEW',
    defaults: [
      { keys: ['Meta', 'B'], platform: 'mac' },
      { keys: ['Ctrl', 'B'], platform: 'other' },
    ],
  },
  {
    id: 'find_usages',
    label: 'Find Usages',
    scope: 'PREVIEW',
    defaults: [{ keys: ['Alt', 'F7'], platform: 'all' }],
  },
  {
    id: 'navigation_back',
    label: 'Navigate Back',
    scope: 'AgentSpace',
    defaults: [
      { keys: ['Meta', '['], platform: 'mac' },
      { keys: ['Ctrl', 'Alt', 'ArrowLeft'], platform: 'other' },
    ],
  },
  {
    id: 'navigation_forward',
    label: 'Navigate Forward',
    scope: 'AgentSpace',
    defaults: [
      { keys: ['Meta', ']'], platform: 'mac' },
      { keys: ['Ctrl', 'Alt', 'ArrowRight'], platform: 'other' },
    ],
  },
  {
    id: 'focus_files',
    label: 'Focus Files',
    scope: 'AgentSpace',
    defaults: [{ keys: ['Alt', '1'], platform: 'all' }],
  },
  {
    id: 'focus_preview',
    label: 'Focus Preview',
    scope: 'AgentSpace',
    defaults: [{ keys: ['Alt', '2'], platform: 'all' }],
  },
  {
    id: 'focus_terminal',
    label: 'Focus Terminal',
    scope: 'AgentSpace',
    defaults: [{ keys: ['Alt', '3'], platform: 'all' }],
  },
];

const COMMAND_BY_ID = new Map(AGENT_SPACE_SHORTCUT_COMMANDS.map((command) => [command.id, command]));
const COMMAND_IDS = AGENT_SPACE_SHORTCUT_COMMANDS.map((command) => command.id);
const RESERVED_BROWSER_KEYS = new Set(['L', 'R', 'W', 'T', 'N']);
const MODIFIER_KEYS = new Set(['Meta', 'Ctrl', 'Alt', 'Shift']);

export const DEFAULT_AGENT_SPACE_SHORTCUTS: AgentSpaceShortcutSettings = {
  version: 1,
  bindings: Object.fromEntries(
    AGENT_SPACE_SHORTCUT_COMMANDS.map((command) => [command.id, command.defaults.map(cloneBinding)]),
  ) as Record<AgentSpaceShortcutCommandId, ShortcutBinding[]>,
};

function cloneBinding(binding: ShortcutBinding): ShortcutBinding {
  return { keys: [...binding.keys], platform: binding.platform };
}

function cloneSettings(settings: AgentSpaceShortcutSettings): AgentSpaceShortcutSettings {
  return {
    version: 1,
    bindings: Object.fromEntries(
      COMMAND_IDS.map((id) => [id, (settings.bindings[id] || []).map(cloneBinding)]),
    ) as Record<AgentSpaceShortcutCommandId, ShortcutBinding[]>,
  };
}

function normalizePlatform(value: unknown): ShortcutPlatform {
  return value === 'mac' || value === 'other' || value === 'all' ? value : 'all';
}

function normalizeKeyName(key: unknown): string {
  const raw = String(key ?? '').trim();
  if (!raw) return '';
  const lower = raw.toLowerCase();
  if (lower === 'cmd' || lower === 'command' || lower === 'meta' || lower === 'os') return 'Meta';
  if (lower === 'control' || lower === 'ctrl') return 'Ctrl';
  if (lower === 'option' || lower === 'alt') return 'Alt';
  if (lower === 'shift') return 'Shift';
  if (lower === 'esc' || lower === 'escape') return 'Escape';
  if (lower === 'space' || lower === 'spacebar' || raw === ' ') return 'Space';
  if (lower === 'left' || lower === 'arrowleft') return 'ArrowLeft';
  if (lower === 'right' || lower === 'arrowright') return 'ArrowRight';
  if (lower === 'up' || lower === 'arrowup') return 'ArrowUp';
  if (lower === 'down' || lower === 'arrowdown') return 'ArrowDown';
  if (/^key[a-z]$/i.test(raw)) return raw.slice(3).toUpperCase();
  if (/^digit[0-9]$/i.test(raw)) return raw.slice(5);
  if (/^f([1-9]|1[0-9]|2[0-4])$/i.test(raw)) return raw.toUpperCase();
  if (raw.length === 1 && /^[a-z]$/i.test(raw)) return raw.toUpperCase();
  return raw;
}

function orderedKeys(keys: string[]): string[] {
  const modifiers = ['Meta', 'Ctrl', 'Alt', 'Shift'];
  const out: string[] = [];
  for (const modifier of modifiers) {
    if (keys.includes(modifier)) out.push(modifier);
  }
  for (const key of keys) {
    if (!modifiers.includes(key) && !out.includes(key)) out.push(key);
  }
  return out;
}

export function normalizeShortcutBinding(raw: unknown): ShortcutBinding | null {
  if (!raw || typeof raw !== 'object') return null;
  const src = raw as { keys?: unknown; platform?: unknown };
  if (!Array.isArray(src.keys)) return null;
  const keys = src.keys.map(normalizeKeyName).filter(Boolean);
  if (keys.length === 0) return null;
  if (keys.length === 2 && keys[0] === 'Shift' && keys[1] === 'Shift') {
    return { keys: ['Shift', 'Shift'], platform: normalizePlatform(src.platform) };
  }
  return { keys: orderedKeys([...new Set(keys)]), platform: normalizePlatform(src.platform) };
}

export function normalizeShortcutSettings(raw: Partial<AgentSpaceShortcutSettings> | null | undefined): AgentSpaceShortcutSettings {
  const src = raw && typeof raw === 'object' ? raw : {};
  const rawBindings = src.bindings && typeof src.bindings === 'object' ? src.bindings : {};
  const bindings = Object.fromEntries(
    COMMAND_IDS.map((id) => {
      const value = (rawBindings as Partial<Record<AgentSpaceShortcutCommandId, unknown>>)[id];
      const normalized = Array.isArray(value)
        ? value.map(normalizeShortcutBinding).filter((binding): binding is ShortcutBinding => !!binding)
        : DEFAULT_AGENT_SPACE_SHORTCUTS.bindings[id].map(cloneBinding);
      return [id, normalized];
    }),
  ) as Record<AgentSpaceShortcutCommandId, ShortcutBinding[]>;
  return { version: 1, bindings };
}

export function loadAgentSpaceShortcuts(): AgentSpaceShortcutSettings {
  try {
    const raw = localStorage.getItem(AGENT_SPACE_SHORTCUTS_KEY);
    return normalizeShortcutSettings(raw ? JSON.parse(raw) as Partial<AgentSpaceShortcutSettings> : null);
  } catch (_) {
    return normalizeShortcutSettings(null);
  }
}

export function saveAgentSpaceShortcuts(
  settings: Partial<AgentSpaceShortcutSettings>,
  source = 'agent-space',
): AgentSpaceShortcutSettings {
  const next = normalizeShortcutSettings(settings);
  try {
    localStorage.setItem(AGENT_SPACE_SHORTCUTS_KEY, JSON.stringify(next));
  } catch (_) {
    // ignore storage failures
  }
  try {
    window.dispatchEvent(new CustomEvent(AGENT_SPACE_SHORTCUTS_EVENT, { detail: { shortcuts: next, source } }));
  } catch (_) {
    // ignore event failures
  }
  return next;
}

function platformMatches(bindingPlatform: ShortcutPlatform, platform: ShortcutPlatform): boolean {
  if (bindingPlatform === 'all') return true;
  if (platform === 'all') return true;
  return bindingPlatform === platform;
}

function platformsOverlap(left: ShortcutPlatform, right: ShortcutPlatform): boolean {
  return left === 'all' || right === 'all' || left === right;
}

export function resolveShortcutBinding(
  settings: AgentSpaceShortcutSettings,
  commandId: AgentSpaceShortcutCommandId,
  platform: ShortcutPlatform,
): ShortcutBinding | null {
  const bindings = normalizeShortcutSettings(settings).bindings[commandId] || [];
  const exact = bindings.find((binding) => binding.platform === platform);
  if (exact) return cloneBinding(exact);
  const all = bindings.find((binding) => platformMatches(binding.platform, platform));
  return all ? cloneBinding(all) : null;
}

function shortcutSignature(binding: ShortcutBinding): string {
  return binding.keys.join('+');
}

export function findShortcutConflict(
  settings: AgentSpaceShortcutSettings,
  commandId: AgentSpaceShortcutCommandId,
  binding: ShortcutBinding,
): ShortcutConflict | null {
  const normalizedBinding = normalizeShortcutBinding(binding);
  if (!normalizedBinding) return null;
  const normalizedSettings = normalizeShortcutSettings(settings);
  const signature = shortcutSignature(normalizedBinding);
  for (const otherId of COMMAND_IDS) {
    if (otherId === commandId) continue;
    for (const otherBinding of normalizedSettings.bindings[otherId] || []) {
      if (shortcutSignature(otherBinding) !== signature) continue;
      if (!platformsOverlap(otherBinding.platform, normalizedBinding.platform)) continue;
      return {
        commandId: otherId,
        label: COMMAND_BY_ID.get(otherId)?.label || otherId,
        binding: cloneBinding(otherBinding),
      };
    }
  }
  return null;
}

function isDoubleShift(binding: ShortcutBinding): boolean {
  return binding.keys.length === 2 && binding.keys[0] === 'Shift' && binding.keys[1] === 'Shift';
}

function isFunctionKey(key: string): boolean {
  return /^F([1-9]|1[0-9]|2[0-4])$/.test(key);
}

function hasModifier(binding: ShortcutBinding): boolean {
  return binding.keys.some((key) => MODIFIER_KEYS.has(key));
}

function hasNonModifier(binding: ShortcutBinding): boolean {
  return binding.keys.some((key) => !MODIFIER_KEYS.has(key));
}

export function reservedBrowserShortcutReason(binding: ShortcutBinding): 'reserved_browser_shortcut' | null {
  const normalized = normalizeShortcutBinding(binding);
  if (!normalized || normalized.keys.length < 2) return null;
  const baseKey = normalized.keys[normalized.keys.length - 1];
  if (!RESERVED_BROWSER_KEYS.has(baseKey)) return null;
  const modifierCount = normalized.keys.slice(0, -1).filter((key) => MODIFIER_KEYS.has(key)).length;
  if (modifierCount !== 1) return null;
  return normalized.keys.includes('Meta') || normalized.keys.includes('Ctrl') ? 'reserved_browser_shortcut' : null;
}

export function validateShortcutBinding(
  binding: ShortcutBinding,
  settings?: AgentSpaceShortcutSettings,
  commandId?: AgentSpaceShortcutCommandId,
): ShortcutValidationResult {
  const normalized = normalizeShortcutBinding(binding);
  if (!normalized) return { ok: false, reason: 'empty' };
  if (reservedBrowserShortcutReason(normalized)) return { ok: false, reason: 'reserved_browser_shortcut' };
  if (!isDoubleShift(normalized) && !hasNonModifier(normalized)) {
    return { ok: false, reason: 'plain_key_without_modifier' };
  }
  if (!isDoubleShift(normalized) && !hasModifier(normalized) && !isFunctionKey(normalized.keys[0] || '')) {
    return { ok: false, reason: 'plain_key_without_modifier' };
  }
  if (settings && commandId) {
    const conflict = findShortcutConflict(settings, commandId, normalized);
    if (conflict) return { ok: false, reason: 'conflict', conflict };
  }
  return { ok: true, binding: normalized };
}

export function setShortcutBinding(
  settings: AgentSpaceShortcutSettings,
  commandId: AgentSpaceShortcutCommandId,
  binding: ShortcutBinding,
  platform?: ShortcutPlatform,
): AgentSpaceShortcutSettings {
  const normalized = normalizeShortcutBinding({ ...binding, platform: platform || binding.platform });
  const next = cloneSettings(normalizeShortcutSettings(settings));
  if (!normalized) return next;
  const replacePlatform = platform || normalized.platform;
  next.bindings[commandId] = [
    ...(next.bindings[commandId] || []).filter((existing) => !platformsOverlap(existing.platform, replacePlatform)),
    normalized,
  ];
  return next;
}

export function clearShortcutBinding(
  settings: AgentSpaceShortcutSettings,
  commandId: AgentSpaceShortcutCommandId,
  platform: ShortcutPlatform,
): AgentSpaceShortcutSettings {
  const next = cloneSettings(normalizeShortcutSettings(settings));
  next.bindings[commandId] = (next.bindings[commandId] || []).filter((binding) => !platformsOverlap(binding.platform, platform));
  return next;
}

export function resetShortcutBinding(
  settings: AgentSpaceShortcutSettings,
  commandId: AgentSpaceShortcutCommandId,
  platform: ShortcutPlatform,
): AgentSpaceShortcutSettings {
  const next = clearShortcutBinding(settings, commandId, platform);
  const defaults = DEFAULT_AGENT_SPACE_SHORTCUTS.bindings[commandId] || [];
  next.bindings[commandId] = [
    ...(next.bindings[commandId] || []),
    ...defaults.filter((binding) => platformMatches(binding.platform, platform)).map(cloneBinding),
  ];
  return next;
}

export function normalizeShortcutKeyEvent(event: ShortcutKeyEventLike): ShortcutBinding | null {
  const baseKey = normalizeKeyName(event.code && /^Key[A-Z]$|^Digit[0-9]$/i.test(event.code) ? event.code : event.key);
  if (!baseKey) return null;
  const keys: string[] = [];
  if (event.metaKey) keys.push('Meta');
  if (event.ctrlKey) keys.push('Ctrl');
  if (event.altKey) keys.push('Alt');
  if (event.shiftKey) keys.push('Shift');
  if (!MODIFIER_KEYS.has(baseKey)) keys.push(baseKey);
  return normalizeShortcutBinding({ keys, platform: 'all' });
}

export function shortcutRecordingActionFromEvent(
  event: ShortcutKeyEventLike,
  captured?: ShortcutBinding | null,
): ShortcutRecordingAction {
  const key = normalizeKeyName(event.key);
  if (key === 'Escape') return { action: 'cancel' };
  if (key === 'Backspace') return { action: 'clear' };
  if (key === 'Enter') {
    const binding = captured ? normalizeShortcutBinding(captured) : null;
    return binding ? { action: 'confirm', binding } : { action: 'ignore' };
  }
  const binding = normalizeShortcutKeyEvent(event);
  return binding ? { action: 'capture', binding } : { action: 'ignore' };
}

export function formatShortcutBinding(binding: ShortcutBinding | null | undefined, platform: ShortcutPlatform): string {
  const normalized = binding ? normalizeShortcutBinding(binding) : null;
  if (!normalized) return 'Unassigned';
  if (isDoubleShift(normalized)) return 'Double Shift';
  return normalized.keys.map((key) => {
    if (key === 'Meta') return platform === 'mac' ? 'Cmd' : 'Meta';
    if (key === 'ArrowLeft') return 'Left';
    if (key === 'ArrowRight') return 'Right';
    if (key === 'ArrowUp') return 'Up';
    if (key === 'ArrowDown') return 'Down';
    if (key === 'Space') return 'Space';
    return key;
  }).join('+');
}

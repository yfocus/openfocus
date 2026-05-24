/* SPDX-License-Identifier: Apache-2.0 */
import {
  AGENT_SPACE_SHORTCUTS_EVENT,
  AGENT_SPACE_SHORTCUTS_KEY,
  AGENT_SPACE_SHORTCUT_COMMANDS,
  DEFAULT_AGENT_SPACE_SHORTCUTS,
  clearShortcutBinding,
  findShortcutConflict,
  formatShortcutBinding,
  loadAgentSpaceShortcuts,
  normalizeShortcutBinding,
  normalizeShortcutKeyEvent,
  normalizeShortcutSettings,
  reservedBrowserShortcutReason,
  resetShortcutBinding,
  resolveShortcutBinding,
  saveAgentSpaceShortcuts,
  setShortcutBinding,
  shortcutRecordingActionFromEvent,
  validateShortcutBinding,
  type AgentSpaceShortcutSettings,
  type ShortcutKeyEventLike,
} from '../src/lib/agentSpaceShortcuts.js';

type TestCase = {
  name: string;
  run: () => void;
};

type StoredValue = string | null;

type FakeLocalStorage = {
  stored: StoredValue;
  writes: Array<{ key: string; value: string }>;
  failGet: boolean;
  failSet: boolean;
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
};

type FakeWindow = {
  events: Array<{ type: string; detail: unknown }>;
  failDispatch: boolean;
  dispatchEvent: (event: CustomEvent) => boolean;
};

class TestCustomEvent<T = unknown> {
  readonly type: string;
  readonly detail: T;

  constructor(type: string, init?: { detail?: T }) {
    this.type = type;
    this.detail = init?.detail as T;
  }
}

function assertEqual<T>(actual: T, expected: T, message?: string): void {
  if (actual !== expected) {
    throw new Error(`${message ? `${message}: ` : ''}expected ${String(expected)}, got ${String(actual)}`);
  }
}

function assertDeepEqual(actual: unknown, expected: unknown, message?: string): void {
  const actualJson = JSON.stringify(actual);
  const expectedJson = JSON.stringify(expected);
  if (actualJson !== expectedJson) {
    throw new Error(`${message ? `${message}: ` : ''}expected ${expectedJson}, got ${actualJson}`);
  }
}

function assertOk(value: unknown, message?: string): void {
  if (!value) throw new Error(message || 'expected truthy value');
}

function installBrowserFakes(options: { stored?: StoredValue; failGet?: boolean; failSet?: boolean; failDispatch?: boolean } = {}): {
  storage: FakeLocalStorage;
  win: FakeWindow;
} {
  const storage: FakeLocalStorage = {
    stored: options.stored ?? null,
    writes: [],
    failGet: options.failGet ?? false,
    failSet: options.failSet ?? false,
    getItem(key: string): string | null {
      if (this.failGet) throw new Error('get failed');
      assertEqual(key, AGENT_SPACE_SHORTCUTS_KEY, 'storage key');
      return this.stored;
    },
    setItem(key: string, value: string): void {
      if (this.failSet) throw new Error('set failed');
      this.writes.push({ key, value });
      this.stored = value;
    },
  };
  const win: FakeWindow = {
    events: [],
    failDispatch: options.failDispatch ?? false,
    dispatchEvent(event: CustomEvent): boolean {
      if (this.failDispatch) throw new Error('dispatch failed');
      this.events.push({ type: event.type, detail: event.detail });
      return true;
    },
  };
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage });
  Object.defineProperty(globalThis, 'window', { configurable: true, value: win });
  Object.defineProperty(globalThis, 'CustomEvent', { configurable: true, value: TestCustomEvent });
  return { storage, win };
}

function eventLike(key: string, init: Partial<ShortcutKeyEventLike> = {}): ShortcutKeyEventLike {
  return {
    key,
    code: init.code,
    metaKey: init.metaKey ?? false,
    ctrlKey: init.ctrlKey ?? false,
    altKey: init.altKey ?? false,
    shiftKey: init.shiftKey ?? false,
    repeat: init.repeat ?? false,
  };
}

const tests: TestCase[] = [
  {
    name: 'exposes all V1 shortcut commands and platform defaults',
    run: () => {
      assertDeepEqual(
        AGENT_SPACE_SHORTCUT_COMMANDS.map((command) => command.id),
        [
          'search_everywhere',
          'find_in_files',
          'go_to_definition',
          'find_usages',
          'navigation_back',
          'navigation_forward',
          'focus_files',
          'focus_preview',
          'focus_terminal',
        ],
      );
      assertEqual(formatShortcutBinding(resolveShortcutBinding(DEFAULT_AGENT_SPACE_SHORTCUTS, 'search_everywhere', 'mac'), 'mac'), 'Double Shift');
      assertEqual(formatShortcutBinding(resolveShortcutBinding(DEFAULT_AGENT_SPACE_SHORTCUTS, 'find_in_files', 'mac'), 'mac'), 'Cmd+Shift+F');
      assertEqual(formatShortcutBinding(resolveShortcutBinding(DEFAULT_AGENT_SPACE_SHORTCUTS, 'find_in_files', 'other'), 'other'), 'Ctrl+Shift+F');
      assertEqual(formatShortcutBinding(resolveShortcutBinding(DEFAULT_AGENT_SPACE_SHORTCUTS, 'navigation_back', 'mac'), 'mac'), 'Cmd+[');
      assertEqual(formatShortcutBinding(resolveShortcutBinding(DEFAULT_AGENT_SPACE_SHORTCUTS, 'navigation_back', 'other'), 'other'), 'Ctrl+Alt+Left');
    },
  },
  {
    name: 'normalizes stored shortcut data and backfills missing commands',
    run: () => {
      const normalized = normalizeShortcutSettings({
        version: 2,
        bindings: {
          search_everywhere: [{ keys: [' shift ', 'Shift'], platform: 'all' }],
          find_in_files: [{ keys: ['Control', ' shift ', 'KeyF'], platform: 'other' }],
          unknown: [{ keys: ['Meta', 'L'], platform: 'mac' }],
        },
      } as unknown as AgentSpaceShortcutSettings);

      assertEqual(normalized.version, 1);
      assertDeepEqual(normalized.bindings.search_everywhere, [{ keys: ['Shift', 'Shift'], platform: 'all' }]);
      assertDeepEqual(normalized.bindings.find_in_files, [{ keys: ['Ctrl', 'Shift', 'F'], platform: 'other' }]);
      assertDeepEqual(normalized.bindings.go_to_definition, DEFAULT_AGENT_SPACE_SHORTCUTS.bindings.go_to_definition);
      assertEqual(Object.prototype.hasOwnProperty.call(normalized.bindings, 'unknown'), false);
    },
  },
  {
    name: 'loads and saves localStorage-backed shortcuts',
    run: () => {
      const { storage, win } = installBrowserFakes({
        stored: JSON.stringify({ bindings: { focus_files: [{ keys: ['Alt', '4'], platform: 'all' }] } }),
      });
      const loaded = loadAgentSpaceShortcuts();
      assertEqual(formatShortcutBinding(resolveShortcutBinding(loaded, 'focus_files', 'other'), 'other'), 'Alt+4');

      const saved = saveAgentSpaceShortcuts(loaded, 'test-source');
      assertDeepEqual(storage.writes, [{ key: AGENT_SPACE_SHORTCUTS_KEY, value: JSON.stringify(saved) }]);
      assertDeepEqual(win.events, [{ type: AGENT_SPACE_SHORTCUTS_EVENT, detail: { shortcuts: saved, source: 'test-source' } }]);

      installBrowserFakes({ stored: '{bad json', failSet: true, failDispatch: true });
      assertDeepEqual(loadAgentSpaceShortcuts(), DEFAULT_AGENT_SPACE_SHORTCUTS);
      assertDeepEqual(saveAgentSpaceShortcuts({ bindings: {} } as AgentSpaceShortcutSettings), DEFAULT_AGENT_SPACE_SHORTCUTS);
    },
  },
  {
    name: 'sets, resets, clears, and detects conflicts by effective platform',
    run: () => {
      const custom = setShortcutBinding(DEFAULT_AGENT_SPACE_SHORTCUTS, 'focus_files', { keys: ['Alt', '5'], platform: 'all' });
      assertEqual(formatShortcutBinding(resolveShortcutBinding(custom, 'focus_files', 'mac'), 'mac'), 'Alt+5');
      assertDeepEqual(findShortcutConflict(custom, 'focus_preview', { keys: ['Alt', '5'], platform: 'all' })?.commandId, 'focus_files');

      const cleared = clearShortcutBinding(custom, 'focus_files', 'mac');
      assertEqual(resolveShortcutBinding(cleared, 'focus_files', 'mac'), null);

      const reset = resetShortcutBinding(cleared, 'focus_files', 'mac');
      assertEqual(formatShortcutBinding(resolveShortcutBinding(reset, 'focus_files', 'mac'), 'mac'), 'Alt+1');

      const macOnly = setShortcutBinding(DEFAULT_AGENT_SPACE_SHORTCUTS, 'find_in_files', { keys: ['Meta', 'P'], platform: 'mac' }, 'mac');
      assertEqual(formatShortcutBinding(resolveShortcutBinding(macOnly, 'find_in_files', 'mac'), 'mac'), 'Cmd+P');
      assertEqual(formatShortcutBinding(resolveShortcutBinding(macOnly, 'find_in_files', 'other'), 'other'), 'Ctrl+Shift+F');
    },
  },
  {
    name: 'rejects reserved browser shortcuts and invalid plain text chords',
    run: () => {
      assertEqual(reservedBrowserShortcutReason({ keys: ['Meta', 'L'], platform: 'mac' }), 'reserved_browser_shortcut');
      assertEqual(validateShortcutBinding({ keys: ['Meta', 'L'], platform: 'mac' }).ok, false);
      const plainKey = validateShortcutBinding({ keys: ['A'], platform: 'all' });
      assertEqual(plainKey.ok ? '' : plainKey.reason, 'plain_key_without_modifier');
      assertEqual(validateShortcutBinding({ keys: ['F7'], platform: 'all' }).ok, true);
      assertEqual(validateShortcutBinding({ keys: ['Shift', 'Shift'], platform: 'all' }).ok, true);
      assertEqual(validateShortcutBinding({ keys: ['Ctrl', 'Shift', 'F'], platform: 'all' }).ok, true);
      const conflict = validateShortcutBinding({ keys: ['Alt', '2'], platform: 'all' }, DEFAULT_AGENT_SPACE_SHORTCUTS, 'focus_files');
      assertEqual(
        conflict.ok ? '' : conflict.reason,
        'conflict',
      );
    },
  },
  {
    name: 'rejects modifier-only shortcut bindings',
    run: () => {
      for (const keys of [['Ctrl'], ['Alt'], ['Meta'], ['Shift'], ['Ctrl', 'Shift'], ['Meta', 'Alt', 'Shift']]) {
        const validation = validateShortcutBinding({ keys, platform: 'all' });
        assertEqual(validation.ok ? '' : validation.reason, 'plain_key_without_modifier', `${keys.join('+')} validation`);
      }
      assertEqual(validateShortcutBinding({ keys: ['Shift', 'Shift'], platform: 'all' }).ok, true, 'Double Shift remains valid');
      assertEqual(validateShortcutBinding({ keys: ['F7'], platform: 'all' }).ok, true, 'function key remains valid');
      assertEqual(validateShortcutBinding({ keys: ['Ctrl', 'Shift', 'F'], platform: 'all' }).ok, true, 'modified chord remains valid');
    },
  },
  {
    name: 'normalizes key events and shortcut recording control actions',
    run: () => {
      assertDeepEqual(normalizeShortcutBinding({ keys: ['Command', 'shift', 'KeyF'], platform: 'mac' }), {
        keys: ['Meta', 'Shift', 'F'],
        platform: 'mac',
      });
      assertDeepEqual(normalizeShortcutKeyEvent(eventLike('f', { metaKey: true, shiftKey: true, code: 'KeyF' })), {
        keys: ['Meta', 'Shift', 'F'],
        platform: 'all',
      });
      assertDeepEqual(normalizeShortcutKeyEvent(eventLike('ArrowLeft', { ctrlKey: true, altKey: true })), {
        keys: ['Ctrl', 'Alt', 'ArrowLeft'],
        platform: 'all',
      });

      assertDeepEqual(shortcutRecordingActionFromEvent(eventLike('Escape')), { action: 'cancel' });
      assertDeepEqual(shortcutRecordingActionFromEvent(eventLike('Backspace')), { action: 'clear' });
      assertDeepEqual(shortcutRecordingActionFromEvent(eventLike('Enter'), { keys: ['Alt', '3'], platform: 'all' }), {
        action: 'confirm',
        binding: { keys: ['Alt', '3'], platform: 'all' },
      });
      const capture = shortcutRecordingActionFromEvent(eventLike('7', { altKey: true, code: 'Digit7' }));
      assertEqual(capture.action, 'capture');
      assertOk('binding' in capture && capture.binding);
    },
  },
];

let failures = 0;
for (const test of tests) {
  try {
    test.run();
    console.log(`ok - ${test.name}`);
  } catch (error) {
    failures += 1;
    console.error(`not ok - ${test.name}`);
    console.error(error instanceof Error ? error.message : String(error));
  }
}

if (failures) {
  throw new Error(`${failures} frontend AgentSpace shortcuts test(s) failed`);
}

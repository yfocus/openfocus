/* SPDX-License-Identifier: Apache-2.0 */
import {
  AGENT_SPACE_SETTINGS_EVENT,
  AGENT_SPACE_SETTINGS_KEY,
  DEFAULT_AGENT_SPACE_SETTINGS,
  loadAgentSpaceSettings,
  normalizeAgentSpaceSettings,
  saveAgentSpaceSettings,
  type AgentSpaceSettings,
} from '../src/lib/agentSpaceSettings.js';

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
      assertEqual(key, AGENT_SPACE_SETTINGS_KEY, 'storage key');
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

const normalizedCustomSettings: AgentSpaceSettings = {
  filesFontSize: 10,
  previewFontSize: 24,
  terminalFontSize: 14,
  showFiles: false,
  showPreview: true,
  showTerminal: true,
};

const tests: TestCase[] = [
  {
    name: 'normalizes defaults, clamps font sizes, and only treats explicit false as false',
    run: () => {
      assertDeepEqual(normalizeAgentSpaceSettings(null), DEFAULT_AGENT_SPACE_SETTINGS);
      assertDeepEqual(
        normalizeAgentSpaceSettings({
          filesFontSize: 1,
          previewFontSize: 99,
          terminalFontSize: 13.6,
          showFiles: false,
          showPreview: 0 as unknown as boolean,
          showTerminal: null as unknown as boolean,
        }),
        normalizedCustomSettings,
      );
      assertDeepEqual(
        normalizeAgentSpaceSettings({ filesFontSize: 'bad' as unknown as number }),
        DEFAULT_AGENT_SPACE_SETTINGS,
      );
    },
  },
  {
    name: 'loads defaults for missing storage and bad JSON',
    run: () => {
      installBrowserFakes();
      assertDeepEqual(loadAgentSpaceSettings(), DEFAULT_AGENT_SPACE_SETTINGS);

      installBrowserFakes({ stored: '{bad json' });
      assertDeepEqual(loadAgentSpaceSettings(), DEFAULT_AGENT_SPACE_SETTINGS);
    },
  },
  {
    name: 'loads and normalizes stored settings',
    run: () => {
      installBrowserFakes({
        stored: JSON.stringify({ filesFontSize: 8, previewFontSize: 30, terminalFontSize: 13.8, showFiles: false }),
      });
      assertDeepEqual(loadAgentSpaceSettings(), normalizedCustomSettings);
    },
  },
  {
    name: 'saves normalized JSON and dispatches settings changed event with source',
    run: () => {
      const { storage, win } = installBrowserFakes();
      const saved = saveAgentSpaceSettings(
        { filesFontSize: 7, previewFontSize: 25, terminalFontSize: 13.8, showFiles: false },
        'test-source',
      );

      assertDeepEqual(saved, normalizedCustomSettings);
      assertDeepEqual(storage.writes, [{ key: AGENT_SPACE_SETTINGS_KEY, value: JSON.stringify(normalizedCustomSettings) }]);
      assertDeepEqual(win.events, [
        { type: AGENT_SPACE_SETTINGS_EVENT, detail: { settings: normalizedCustomSettings, source: 'test-source' } },
      ]);
    },
  },
  {
    name: 'ignores storage and event failures while returning normalized settings',
    run: () => {
      installBrowserFakes({ failSet: true, failDispatch: true });
      assertDeepEqual(
        saveAgentSpaceSettings({ filesFontSize: 24, previewFontSize: 10, terminalFontSize: Number.NaN }),
        {
          filesFontSize: 24,
          previewFontSize: 10,
          terminalFontSize: DEFAULT_AGENT_SPACE_SETTINGS.terminalFontSize,
          showFiles: true,
          showPreview: true,
          showTerminal: true,
        },
      );
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
  throw new Error(`${failures} frontend AgentSpace settings test(s) failed`);
}

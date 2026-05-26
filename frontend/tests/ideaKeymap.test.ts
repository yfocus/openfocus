/* SPDX-License-Identifier: Apache-2.0 */
import {
  ACTIVE_TERMINAL_IFRAME_SELECTOR,
  commandFromShortcutEvent,
  createDoubleShiftDetector,
  detectShortcutPlatform,
  findActiveTerminalIframe,
  isEditableShortcutTarget,
  isAgentSpaceShortcutCommandImplemented,
  isPreviewGoToDefinitionShortcutTarget,
  isTerminalShortcutTarget,
  shouldIgnoreAgentSpaceShortcut,
  shouldRunPreviewCodeShortcut,
  shouldRunPreviewGoToDefinitionShortcut,
  shortcutEventMatchesCommand,
  shortcutEventMatchesBinding,
} from '../src/lib/ideaKeymap.js';
import {
  DEFAULT_AGENT_SPACE_SHORTCUTS,
  AGENT_SPACE_SHORTCUT_COMMANDS,
  type ShortcutKeyEventLike,
} from '../src/lib/agentSpaceShortcuts.js';

type TestCase = {
  name: string;
  run: () => void;
};

type FakeElement = {
  tagName: string;
  parentElement: FakeElement | null;
  attrs: Record<string, string>;
  isContentEditable?: boolean;
  getAttribute: (name: string) => string | null;
};

function assertEqual<T>(actual: T, expected: T, message?: string): void {
  if (actual !== expected) {
    throw new Error(`${message ? `${message}: ` : ''}expected ${String(expected)}, got ${String(actual)}`);
  }
}

function keyEvent(key: string, init: Partial<ShortcutKeyEventLike> = {}): ShortcutKeyEventLike {
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

function fakeElement(tagName: string, attrs: Record<string, string> = {}, parentElement: FakeElement | null = null): FakeElement {
  return {
    tagName,
    attrs,
    parentElement,
    getAttribute(name: string): string | null {
      return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    },
  };
}

const tests: TestCase[] = [
  {
    name: 'detects mac and non-mac shortcut platforms',
    run: () => {
      assertEqual(detectShortcutPlatform('MacIntel'), 'mac');
      assertEqual(detectShortcutPlatform('iPad'), 'mac');
      assertEqual(detectShortcutPlatform('Linux x86_64'), 'other');
      assertEqual(detectShortcutPlatform('Windows'), 'other');
    },
  },
  {
    name: 'matches key chords by normalized shortcut signature',
    run: () => {
      assertEqual(
        shortcutEventMatchesBinding(keyEvent('f', { code: 'KeyF', metaKey: true, shiftKey: true }), { keys: ['Meta', 'Shift', 'F'], platform: 'mac' }),
        true,
      );
      assertEqual(
        shortcutEventMatchesBinding(keyEvent('f', { code: 'KeyF', ctrlKey: true, shiftKey: true }), { keys: ['Meta', 'Shift', 'F'], platform: 'mac' }),
        false,
      );
      assertEqual(
        shortcutEventMatchesBinding(keyEvent('Shift', { shiftKey: true }), { keys: ['Shift', 'Shift'], platform: 'all' }),
        false,
      );
    },
  },
  {
    name: 'guards editable and terminal shortcut targets',
    run: () => {
      const root = fakeElement('div', { 'data-agent-space-terminal': 'true' });
      const child = fakeElement('iframe', {}, root);
      const input = fakeElement('input');
      const button = fakeElement('button');
      const editable = fakeElement('div', { contenteditable: 'true' });
      const terminalRoot = {
        contains(node: unknown): boolean {
          return node === root || node === child;
        },
      };

      assertEqual(isEditableShortcutTarget(input as unknown as EventTarget), true);
      assertEqual(isEditableShortcutTarget(editable as unknown as EventTarget), true);
      assertEqual(isEditableShortcutTarget(button as unknown as EventTarget), false);
      assertEqual(isTerminalShortcutTarget(child as unknown as EventTarget, terminalRoot), true);
      assertEqual(isTerminalShortcutTarget(button as unknown as EventTarget, terminalRoot), false);
      assertEqual(shouldIgnoreAgentSpaceShortcut({ ...keyEvent('f'), defaultPrevented: true }, { target: button as unknown as EventTarget }), true);
      assertEqual(shouldIgnoreAgentSpaceShortcut(keyEvent('f'), { target: input as unknown as EventTarget }), true);
      assertEqual(shouldIgnoreAgentSpaceShortcut(keyEvent('f'), { activeElement: child as unknown as EventTarget, terminalRoot }), true);
      assertEqual(shouldIgnoreAgentSpaceShortcut(keyEvent('f'), { target: button as unknown as EventTarget, terminalRoot }), false);
    },
  },
  {
    name: 'guards Go to Definition shortcut to preview code targets',
    run: () => {
      const previewHost = fakeElement('div', { 'data-agent-space-preview-code': 'true', role: 'textbox' });
      const cmLine = fakeElement('div', { class: 'cm-line' }, previewHost);
      const outsideTextbox = fakeElement('div', { role: 'textbox' });
      const filesPane = fakeElement('div', { 'data-agent-space-pane': 'files' });
      const settingsColumn = fakeElement('div', { class: 'agent-space-settings-column' });
      const terminalRoot = fakeElement('div', { 'data-agent-space-terminal': 'true' });
      const terminalChild = fakeElement('div', {}, terminalRoot);
      const rootGuard = {
        contains(node: unknown): boolean {
          return node === previewHost || node === cmLine;
        },
      };

      assertEqual(isEditableShortcutTarget(cmLine as unknown as EventTarget), true);
      assertEqual(isPreviewGoToDefinitionShortcutTarget(cmLine as unknown as EventTarget), true);
      assertEqual(isPreviewGoToDefinitionShortcutTarget(previewHost as unknown as EventTarget), true);
      assertEqual(isPreviewGoToDefinitionShortcutTarget(outsideTextbox as unknown as EventTarget), false);
      assertEqual(isPreviewGoToDefinitionShortcutTarget(filesPane as unknown as EventTarget), false);
      assertEqual(isPreviewGoToDefinitionShortcutTarget(settingsColumn as unknown as EventTarget), false);
      assertEqual(isPreviewGoToDefinitionShortcutTarget(terminalRoot as unknown as EventTarget), false);
      assertEqual(isPreviewGoToDefinitionShortcutTarget(cmLine as unknown as EventTarget, rootGuard), true);
      assertEqual(
        shortcutEventMatchesCommand(
          keyEvent('B', { code: 'KeyB', metaKey: true }),
          DEFAULT_AGENT_SPACE_SHORTCUTS,
          'mac',
          'go_to_definition',
        ),
        true,
      );
      assertEqual(
        shortcutEventMatchesCommand(
          keyEvent('F7', { code: 'F7', altKey: true }),
          DEFAULT_AGENT_SPACE_SHORTCUTS,
          'mac',
          'find_usages',
        ),
        true,
      );
      assertEqual(
        shouldRunPreviewGoToDefinitionShortcut({
          target: cmLine as unknown as EventTarget,
          activeElement: cmLine as unknown as EventTarget,
        }),
        true,
      );
      assertEqual(
        shouldRunPreviewGoToDefinitionShortcut({
          target: outsideTextbox as unknown as EventTarget,
          activeElement: outsideTextbox as unknown as EventTarget,
        }),
        false,
      );
      assertEqual(
        shouldRunPreviewGoToDefinitionShortcut({
          target: terminalChild as unknown as EventTarget,
          activeElement: cmLine as unknown as EventTarget,
          terminalRoot: {
            contains(node: unknown): boolean {
              return node === terminalRoot || node === terminalChild;
            },
          },
        }),
        false,
      );
      assertEqual(
        shouldRunPreviewGoToDefinitionShortcut({
          target: filesPane as unknown as EventTarget,
          activeElement: cmLine as unknown as EventTarget,
        }),
        false,
      );
      assertEqual(
        shouldRunPreviewGoToDefinitionShortcut({
          target: null,
          activeElement: cmLine as unknown as EventTarget,
        }),
        true,
      );
      assertEqual(
        shouldRunPreviewGoToDefinitionShortcut({
          target: filesPane as unknown as EventTarget,
          activeElement: settingsColumn as unknown as EventTarget,
        }),
        false,
      );
      assertEqual(
        shouldRunPreviewGoToDefinitionShortcut({
          target: fakeElement('body') as unknown as EventTarget,
          activeElement: fakeElement('body') as unknown as EventTarget,
          hasPreviewSelection: true,
        }),
        true,
      );
      assertEqual(
        shouldRunPreviewGoToDefinitionShortcut({
          target: filesPane as unknown as EventTarget,
          activeElement: fakeElement('body') as unknown as EventTarget,
          hasPreviewSelection: true,
        }),
        false,
      );
      assertEqual(
        shouldRunPreviewGoToDefinitionShortcut({
          target: fakeElement('body') as unknown as EventTarget,
          activeElement: terminalChild as unknown as EventTarget,
          terminalRoot: {
            contains(node: unknown): boolean {
              return node === terminalRoot || node === terminalChild;
            },
          },
          hasPreviewSelection: true,
        }),
        false,
      );
      assertEqual(
        shouldRunPreviewCodeShortcut({
          target: cmLine as unknown as EventTarget,
          activeElement: cmLine as unknown as EventTarget,
        }),
        true,
      );
      assertEqual(
        shouldRunPreviewCodeShortcut({
          target: outsideTextbox as unknown as EventTarget,
          activeElement: cmLine as unknown as EventTarget,
        }),
        false,
      );
      assertEqual(
        shouldRunPreviewCodeShortcut({
          target: terminalChild as unknown as EventTarget,
          activeElement: cmLine as unknown as EventTarget,
          terminalRoot: {
            contains(node: unknown): boolean {
              return node === terminalRoot || node === terminalChild;
            },
          },
        }),
        false,
      );
      assertEqual(
        shouldRunPreviewCodeShortcut({
          target: filesPane as unknown as EventTarget,
          activeElement: cmLine as unknown as EventTarget,
        }),
        false,
      );
      assertEqual(
        shouldRunPreviewCodeShortcut({
          target: filesPane as unknown as EventTarget,
          activeElement: settingsColumn as unknown as EventTarget,
        }),
        false,
      );
    },
  },
  {
    name: 'selects active terminal iframe using terminal panel DOM classes',
    run: () => {
      const activeFrame = { id: 'active' };
      const fallbackFrame = { id: 'fallback' };
      const selectors: string[] = [];
      const root = {
        querySelector(selector: string): { id: string } | null {
          selectors.push(selector);
          if (selector === ACTIVE_TERMINAL_IFRAME_SELECTOR) return activeFrame;
          if (selector === 'iframe') return fallbackFrame;
          return null;
        },
      };

      assertEqual(ACTIVE_TERMINAL_IFRAME_SELECTOR, '.rt-term:not(.rt-hidden) iframe');
      assertEqual(findActiveTerminalIframe(root), activeFrame);
      assertEqual(selectors.join('|'), ACTIVE_TERMINAL_IFRAME_SELECTOR);

      const fallbackSelectors: string[] = [];
      assertEqual(
        findActiveTerminalIframe({
          querySelector(selector: string): { id: string } | null {
            fallbackSelectors.push(selector);
            return selector === 'iframe' ? fallbackFrame : null;
          },
        }),
        fallbackFrame,
      );
      assertEqual(fallbackSelectors.join('|'), `${ACTIVE_TERMINAL_IFRAME_SELECTOR}|iframe`);
    },
  },
  {
    name: 'detects Double Shift only as a released sequence',
    run: () => {
      const detector = createDoubleShiftDetector(500);

      assertEqual(detector.keydown(keyEvent('Shift', { code: 'ShiftLeft', shiftKey: true }), 1000), false);
      assertEqual(detector.keydown(keyEvent('Shift', { code: 'ShiftLeft', shiftKey: true }), 1100), false, 'held second Shift must not trigger');
      detector.keyup(keyEvent('Shift', { code: 'ShiftLeft' }));
      assertEqual(detector.keydown(keyEvent('Shift', { code: 'ShiftLeft', shiftKey: true }), 1200), true);

      detector.keyup(keyEvent('Shift', { code: 'ShiftLeft' }));
      assertEqual(detector.keydown(keyEvent('Shift', { code: 'ShiftLeft', shiftKey: true }), 2000), false);
      assertEqual(detector.keydown(keyEvent('F', { code: 'KeyF', shiftKey: true }), 2050), false);
      detector.keyup(keyEvent('Shift', { code: 'ShiftLeft' }));
      assertEqual(detector.keydown(keyEvent('Shift', { code: 'ShiftLeft', shiftKey: true }), 2100), false, 'ordinary Shift chord resets the sequence');
      detector.keyup(keyEvent('Shift', { code: 'ShiftLeft' }));
      assertEqual(detector.keydown(keyEvent('Shift', { code: 'ShiftLeft', shiftKey: true }), 2800), false, 'timeout starts a new sequence');
    },
  },
  {
    name: 'does not trigger Double Shift while either physical Shift remains held',
    run: () => {
      const detector = createDoubleShiftDetector(500);

      assertEqual(detector.keydown(keyEvent('Shift', { code: 'ShiftLeft', shiftKey: true }), 1000), false);
      assertEqual(detector.keydown(keyEvent('Shift', { code: 'ShiftRight', shiftKey: true }), 1100), false, 'alternate Shift press while held must not trigger');
      detector.keyup(keyEvent('Shift', { code: 'ShiftLeft' }));
      assertEqual(detector.keydown(keyEvent('Shift', { code: 'ShiftLeft', shiftKey: true }), 1200), false, 'ShiftRight is still held');
      detector.keyup(keyEvent('Shift', { code: 'ShiftRight' }));
      detector.keyup(keyEvent('Shift', { code: 'ShiftLeft' }));
      assertEqual(detector.keydown(keyEvent('Shift', { code: 'ShiftLeft', shiftKey: true }), 1300), false, 'all Shifts must be released before a new sequence starts');
      detector.keyup(keyEvent('Shift', { code: 'ShiftLeft' }));
      assertEqual(detector.keydown(keyEvent('Shift', { code: 'ShiftRight', shiftKey: true }), 1450), true);
    },
  },
  {
    name: 'dispatches only implemented AgentSpace commands by default',
    run: () => {
      const detector = createDoubleShiftDetector(500);

      assertEqual(commandFromShortcutEvent(keyEvent('Shift', { shiftKey: true }), DEFAULT_AGENT_SPACE_SHORTCUTS, 'mac', detector, undefined, 1000), null);
      detector.keyup(keyEvent('Shift'));
      assertEqual(commandFromShortcutEvent(keyEvent('Shift', { shiftKey: true }), DEFAULT_AGENT_SPACE_SHORTCUTS, 'mac', detector, undefined, 1200), 'search_everywhere');

      assertEqual(
        commandFromShortcutEvent(keyEvent('f', { code: 'KeyF', metaKey: true, shiftKey: true }), DEFAULT_AGENT_SPACE_SHORTCUTS, 'mac', detector, undefined, 1400),
        'find_in_files',
      );
      assertEqual(
        commandFromShortcutEvent(keyEvent('1', { code: 'Digit1', altKey: true }), DEFAULT_AGENT_SPACE_SHORTCUTS, 'other', detector, undefined, 1500),
        'focus_files',
      );
      assertEqual(
        commandFromShortcutEvent(keyEvent('B', { code: 'KeyB', metaKey: true }), DEFAULT_AGENT_SPACE_SHORTCUTS, 'mac', detector, undefined, 1600),
        'go_to_definition',
      );
      assertEqual(
        commandFromShortcutEvent(keyEvent('B', { code: 'KeyB', ctrlKey: true }), DEFAULT_AGENT_SPACE_SHORTCUTS, 'other', detector, undefined, 1650),
        'go_to_definition',
      );
      assertEqual(
        commandFromShortcutEvent(keyEvent('F7', { code: 'F7', altKey: true }), DEFAULT_AGENT_SPACE_SHORTCUTS, 'mac', detector, undefined, 1700),
        'find_usages',
      );
      assertEqual(
        commandFromShortcutEvent(keyEvent('[', { code: 'BracketLeft', metaKey: true }), DEFAULT_AGENT_SPACE_SHORTCUTS, 'mac', detector, undefined, 1800),
        'navigation_back',
      );
      assertEqual(
        commandFromShortcutEvent(keyEvent('ArrowRight', { code: 'ArrowRight', ctrlKey: true, altKey: true }), DEFAULT_AGENT_SPACE_SHORTCUTS, 'other', detector, undefined, 1900),
        'navigation_forward',
      );
      assertEqual(isAgentSpaceShortcutCommandImplemented('find_in_files'), true);
      assertEqual(isAgentSpaceShortcutCommandImplemented('go_to_definition'), true);
      assertEqual(isAgentSpaceShortcutCommandImplemented('find_usages'), true);
      assertEqual(isAgentSpaceShortcutCommandImplemented('navigation_back'), true);
      assertEqual(isAgentSpaceShortcutCommandImplemented('navigation_forward'), true);
      for (const command of AGENT_SPACE_SHORTCUT_COMMANDS) {
        assertEqual(isAgentSpaceShortcutCommandImplemented(command.id), true, `${command.id} implemented`);
      }
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
  throw new Error(`${failures} frontend IDEA keymap test(s) failed`);
}

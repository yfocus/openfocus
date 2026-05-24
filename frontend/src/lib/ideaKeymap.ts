/* SPDX-License-Identifier: Apache-2.0 */
import {
  normalizeShortcutKeyEvent,
  resolveShortcutBinding,
  type AgentSpaceShortcutCommandId,
  type AgentSpaceShortcutSettings,
  type ShortcutBinding,
  type ShortcutKeyEventLike,
  type ShortcutPlatform,
} from './agentSpaceShortcuts.js';

export const AGENT_SPACE_IMPLEMENTED_COMMANDS: AgentSpaceShortcutCommandId[] = [
  'search_everywhere',
  'find_in_files',
  'focus_files',
  'focus_preview',
  'focus_terminal',
];

export const ACTIVE_TERMINAL_IFRAME_SELECTOR = '.rt-term:not(.rt-hidden) iframe';

type ElementLike = {
  tagName?: string;
  isContentEditable?: boolean;
  parentElement?: ElementLike | null;
  closest?: (selector: string) => ElementLike | null;
  getAttribute?: (name: string) => string | null;
};

type NodeLike = {
  contains?: (node: Node | null) => boolean;
};

type QueryRootLike<T> = {
  querySelector: (selector: string) => T | null;
};

export type ShortcutGuardOptions = {
  target?: EventTarget | null;
  activeElement?: EventTarget | null;
  terminalRoot?: NodeLike | null;
};

export type ShortcutEventWithDefault = ShortcutKeyEventLike & {
  defaultPrevented?: boolean;
};

export type DoubleShiftDetector = {
  keydown: (event: ShortcutKeyEventLike, now?: number) => boolean;
  keyup: (event: ShortcutKeyEventLike) => void;
  reset: () => void;
};

function asElementLike(value: unknown): ElementLike | null {
  if (!value || typeof value !== 'object') return null;
  if (typeof Element !== 'undefined' && value instanceof Element) return value as ElementLike;
  const candidate = value as ElementLike;
  return candidate.tagName || candidate.closest || candidate.getAttribute || candidate.parentElement ? candidate : null;
}

function closestElement(target: ElementLike, predicate: (element: ElementLike) => boolean): ElementLike | null {
  if (target.closest) {
    const editable = target.closest('input, textarea, select, [contenteditable], [role="textbox"]');
    if (editable && predicate(editable)) return editable;
  }
  let current: ElementLike | null | undefined = target;
  while (current) {
    if (predicate(current)) return current;
    current = current.parentElement;
  }
  return null;
}

function isEditableElement(element: ElementLike): boolean {
  const tagName = String(element.tagName || '').toLowerCase();
  if (tagName === 'input' || tagName === 'textarea' || tagName === 'select') return true;
  if (element.isContentEditable) return true;
  const rawContentEditable = element.getAttribute?.('contenteditable');
  const contentEditable = String(rawContentEditable || '').toLowerCase();
  if (rawContentEditable !== null && rawContentEditable !== undefined && (contentEditable === '' || contentEditable === 'true' || contentEditable === 'plaintext-only')) return true;
  return String(element.getAttribute?.('role') || '').toLowerCase() === 'textbox';
}

function isTerminalElement(element: ElementLike): boolean {
  const id = String(element.getAttribute?.('id') || '');
  if (id === 'remote-terminal') return true;
  return String(element.getAttribute?.('data-agent-space-terminal') || '').toLowerCase() === 'true';
}

function eventKeyName(event: ShortcutKeyEventLike): string {
  if (event.key === 'Shift') return 'Shift';
  if (event.code === 'ShiftLeft' || event.code === 'ShiftRight') return 'Shift';
  return String(event.key || '');
}

function shiftPhysicalKey(event: ShortcutKeyEventLike): string {
  if (event.code === 'ShiftLeft' || event.code === 'ShiftRight') return event.code;
  return 'ShiftUnknown';
}

function isPlainShiftKeyEvent(event: ShortcutKeyEventLike): boolean {
  return eventKeyName(event) === 'Shift' && !event.metaKey && !event.ctrlKey && !event.altKey && !event.repeat;
}

function isDoubleShiftBinding(binding: ShortcutBinding | null | undefined): boolean {
  return !!binding && binding.keys.length === 2 && binding.keys[0] === 'Shift' && binding.keys[1] === 'Shift';
}

function shortcutSignaturesMatch(left: ShortcutBinding | null, right: ShortcutBinding | null): boolean {
  if (!left || !right) return false;
  if (left.keys.length !== right.keys.length) return false;
  return left.keys.every((key, index) => key === right.keys[index]);
}

export function detectShortcutPlatform(platformText?: string): ShortcutPlatform {
  return /mac|iphone|ipad|ipod/i.test(String(platformText || '')) ? 'mac' : 'other';
}

export function currentShortcutPlatform(navigatorLike: Pick<Navigator, 'platform' | 'userAgent'> = navigator): ShortcutPlatform {
  return detectShortcutPlatform(`${navigatorLike.platform || ''} ${navigatorLike.userAgent || ''}`);
}

export function isEditableShortcutTarget(target: EventTarget | null | undefined): boolean {
  const element = asElementLike(target);
  return !!element && !!closestElement(element, isEditableElement);
}

export function isTerminalShortcutTarget(target: EventTarget | null | undefined, terminalRoot?: NodeLike | null): boolean {
  if (!target) return false;
  if (terminalRoot?.contains?.(target as Node | null)) return true;
  const element = asElementLike(target);
  return !!element && !!closestElement(element, isTerminalElement);
}

export function findActiveTerminalIframe<T>(root: QueryRootLike<T> | null | undefined): T | null {
  if (!root) return null;
  return root.querySelector(ACTIVE_TERMINAL_IFRAME_SELECTOR) || root.querySelector('iframe');
}

export function shouldIgnoreAgentSpaceShortcut(
  event: ShortcutEventWithDefault,
  options: ShortcutGuardOptions = {},
): boolean {
  if (event.defaultPrevented) return true;
  if (isEditableShortcutTarget(options.target)) return true;
  if (isEditableShortcutTarget(options.activeElement)) return true;
  if (isTerminalShortcutTarget(options.target, options.terminalRoot)) return true;
  return isTerminalShortcutTarget(options.activeElement, options.terminalRoot);
}

export function createDoubleShiftDetector(timeoutMs = 500): DoubleShiftDetector {
  let lastShiftAt = 0;
  const downShiftKeys = new Set<string>();
  let blockedUntilAllReleased = false;

  return {
    keydown(event: ShortcutKeyEventLike, now = Date.now()): boolean {
      if (eventKeyName(event) === 'Shift' && event.repeat) {
        downShiftKeys.add(shiftPhysicalKey(event));
        return false;
      }
      if (!isPlainShiftKeyEvent(event)) {
        lastShiftAt = 0;
        downShiftKeys.clear();
        blockedUntilAllReleased = false;
        return false;
      }
      const physicalKey = shiftPhysicalKey(event);
      if (downShiftKeys.size > 0) {
        if (!downShiftKeys.has(physicalKey)) blockedUntilAllReleased = true;
        downShiftKeys.add(physicalKey);
        return false;
      }
      if (blockedUntilAllReleased) return false;
      const elapsed = lastShiftAt ? now - lastShiftAt : Number.POSITIVE_INFINITY;
      downShiftKeys.add(physicalKey);
      lastShiftAt = now;
      if (elapsed > 0 && elapsed <= timeoutMs) {
        lastShiftAt = 0;
        return true;
      }
      return false;
    },
    keyup(event: ShortcutKeyEventLike): void {
      if (eventKeyName(event) !== 'Shift') return;
      downShiftKeys.delete(shiftPhysicalKey(event));
      if (downShiftKeys.size === 0 && blockedUntilAllReleased) {
        lastShiftAt = 0;
        blockedUntilAllReleased = false;
      }
    },
    reset(): void {
      lastShiftAt = 0;
      downShiftKeys.clear();
      blockedUntilAllReleased = false;
    },
  };
}

export function shortcutEventMatchesBinding(event: ShortcutKeyEventLike, binding: ShortcutBinding | null | undefined): boolean {
  if (!binding || isDoubleShiftBinding(binding)) return false;
  return shortcutSignaturesMatch(normalizeShortcutKeyEvent(event), binding);
}

export function isAgentSpaceShortcutCommandImplemented(command: AgentSpaceShortcutCommandId): boolean {
  return AGENT_SPACE_IMPLEMENTED_COMMANDS.includes(command);
}

export function commandFromShortcutEvent(
  event: ShortcutKeyEventLike,
  settings: AgentSpaceShortcutSettings,
  platform: ShortcutPlatform,
  doubleShift: DoubleShiftDetector,
  commandIds: AgentSpaceShortcutCommandId[] = AGENT_SPACE_IMPLEMENTED_COMMANDS,
  now?: number,
): AgentSpaceShortcutCommandId | null {
  const doubleShiftTriggered = doubleShift.keydown(event, now);
  if (doubleShiftTriggered) {
    return commandIds.find((commandId) => isDoubleShiftBinding(resolveShortcutBinding(settings, commandId, platform))) || null;
  }
  if (isPlainShiftKeyEvent(event)) return null;
  return commandIds.find((commandId) => shortcutEventMatchesBinding(event, resolveShortcutBinding(settings, commandId, platform))) || null;
}

/* SPDX-License-Identifier: Apache-2.0 */

export type AgentSpaceNavigationSource =
  | 'files'
  | 'terminal_link'
  | 'search_everywhere'
  | 'find_in_files'
  | 'go_to_definition'
  | 'find_usages'
  | 'markdown_link'
  | 'restore'
  | string;

export type AgentSpaceNavigationHistoryEntry = {
  path: string;
  name: string;
  line?: number;
  column?: number;
  scrollTop?: number;
  topLine?: number;
  source: AgentSpaceNavigationSource;
  ts: number;
};

export type AgentSpaceNavigationHistoryState = {
  backStack: AgentSpaceNavigationHistoryEntry[];
  forwardStack: AgentSpaceNavigationHistoryEntry[];
  current: AgentSpaceNavigationHistoryEntry | null;
};

export type AgentSpaceNavigationResult = {
  state: AgentSpaceNavigationHistoryState;
  entry: AgentSpaceNavigationHistoryEntry | null;
};

export type AgentSpaceNavigationReplay = {
  path: string;
  name: string;
  options: {
    line?: number;
    column?: number;
    scrollTop?: number;
    topLine?: number;
    restoreScroll: true;
    recordHistory: false;
    source: AgentSpaceNavigationSource;
  };
};

export const EMPTY_AGENT_SPACE_NAVIGATION_HISTORY: AgentSpaceNavigationHistoryState = {
  backStack: [],
  forwardStack: [],
  current: null,
};

function positiveInt(value: unknown): number | undefined {
  const parsed = Math.floor(Number(value || 0));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined;
}

function nonNegativeInt(value: unknown): number | undefined {
  const parsed = Math.floor(Number(value || 0));
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined;
}

function cloneEntry(entry: AgentSpaceNavigationHistoryEntry): AgentSpaceNavigationHistoryEntry {
  return { ...entry };
}

function cloneState(state: AgentSpaceNavigationHistoryState): AgentSpaceNavigationHistoryState {
  return {
    backStack: (state.backStack || []).map(cloneEntry),
    forwardStack: (state.forwardStack || []).map(cloneEntry),
    current: state.current ? cloneEntry(state.current) : null,
  };
}

export function navigationHistoryEntriesEqual(
  left: AgentSpaceNavigationHistoryEntry | null | undefined,
  right: AgentSpaceNavigationHistoryEntry | null | undefined,
): boolean {
  if (!left || !right) return false;
  return String(left.path || '') === String(right.path || '')
    && (positiveInt(left.line) || 0) === (positiveInt(right.line) || 0)
    && (positiveInt(left.column) || 0) === (positiveInt(right.column) || 0);
}

export function createNavigationHistoryEntry(input: {
  path?: string;
  name?: string;
  line?: number;
  column?: number;
  scrollTop?: number;
  topLine?: number;
  source?: AgentSpaceNavigationSource;
  ts?: number;
}): AgentSpaceNavigationHistoryEntry | null {
  const path = String(input.path || '').trim();
  if (!path) return null;
  const entry: AgentSpaceNavigationHistoryEntry = {
    path,
    name: String(input.name || '').trim() || path.split('/').pop() || path,
    source: input.source || 'unknown',
    ts: Math.floor(Number(input.ts || 0)) || Date.now(),
  };
  const line = positiveInt(input.line);
  const column = positiveInt(input.column);
  const scrollTop = nonNegativeInt(input.scrollTop);
  const topLine = positiveInt(input.topLine);
  if (line) entry.line = line;
  if (column) entry.column = column;
  if (scrollTop !== undefined) entry.scrollTop = scrollTop;
  if (topLine) entry.topLine = topLine;
  return entry;
}

export function recordNavigationOpen(
  state: AgentSpaceNavigationHistoryState,
  entry: AgentSpaceNavigationHistoryEntry | null | undefined,
): AgentSpaceNavigationHistoryState {
  const nextEntry = entry ? cloneEntry(entry) : null;
  if (!nextEntry) return cloneState(state);
  const current = state.current ? cloneEntry(state.current) : null;
  if (navigationHistoryEntriesEqual(current, nextEntry)) {
    return {
      backStack: (state.backStack || []).map(cloneEntry),
      forwardStack: (state.forwardStack || []).map(cloneEntry),
      current: nextEntry,
    };
  }
  return {
    backStack: current ? [...(state.backStack || []).map(cloneEntry), current] : (state.backStack || []).map(cloneEntry),
    forwardStack: [],
    current: nextEntry,
  };
}

export function navigateBack(
  state: AgentSpaceNavigationHistoryState,
  currentEntry?: AgentSpaceNavigationHistoryEntry | null,
): AgentSpaceNavigationResult {
  const backStack = (state.backStack || []).map(cloneEntry);
  const entry = backStack.pop() || null;
  if (!entry) return { state: cloneState(state), entry: null };
  const current = currentEntry ? cloneEntry(currentEntry) : state.current ? cloneEntry(state.current) : null;
  const forwardStack = (state.forwardStack || []).map(cloneEntry);
  if (current && !navigationHistoryEntriesEqual(current, entry)) forwardStack.push(current);
  return {
    state: { backStack, forwardStack, current: entry },
    entry,
  };
}

export function navigateForward(
  state: AgentSpaceNavigationHistoryState,
  currentEntry?: AgentSpaceNavigationHistoryEntry | null,
): AgentSpaceNavigationResult {
  const forwardStack = (state.forwardStack || []).map(cloneEntry);
  const entry = forwardStack.pop() || null;
  if (!entry) return { state: cloneState(state), entry: null };
  const current = currentEntry ? cloneEntry(currentEntry) : state.current ? cloneEntry(state.current) : null;
  const backStack = (state.backStack || []).map(cloneEntry);
  if (current && !navigationHistoryEntriesEqual(current, entry)) backStack.push(current);
  return {
    state: { backStack, forwardStack, current: entry },
    entry,
  };
}

export function navigationHistoryEntryToPreviewReplay(
  entry: AgentSpaceNavigationHistoryEntry | null | undefined,
): AgentSpaceNavigationReplay | null {
  if (!entry?.path) return null;
  return {
    path: entry.path,
    name: entry.name || entry.path.split('/').pop() || entry.path,
    options: {
      line: positiveInt(entry.line),
      column: positiveInt(entry.column),
      scrollTop: nonNegativeInt(entry.scrollTop),
      topLine: positiveInt(entry.topLine),
      restoreScroll: true,
      recordHistory: false,
      source: entry.source || 'unknown',
    },
  };
}

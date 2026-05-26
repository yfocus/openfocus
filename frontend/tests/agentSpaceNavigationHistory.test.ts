/* SPDX-License-Identifier: Apache-2.0 */
import {
  EMPTY_AGENT_SPACE_NAVIGATION_HISTORY,
  createNavigationHistoryEntry,
  navigateBack,
  navigateForward,
  navigationHistoryEntryToPreviewReplay,
  navigationHistoryEntriesEqual,
  recordNavigationOpen,
  type AgentSpaceNavigationHistoryState,
} from '../src/lib/agentSpaceNavigationHistory.js';

type TestCase = {
  name: string;
  run: () => void;
};

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

function entry(path: string, line: number, source = 'files') {
  const created = createNavigationHistoryEntry({
    path,
    name: path.split('/').pop(),
    line,
    column: 1,
    scrollTop: line * 10,
    topLine: line,
    source,
    ts: line,
  });
  if (!created) throw new Error('expected entry');
  return created;
}

const tests: TestCase[] = [
  {
    name: 'creates normalized history entries',
    run: () => {
      assertDeepEqual(createNavigationHistoryEntry({
        path: ' src/app.ts ',
        line: 4.7,
        column: 2,
        scrollTop: 12.8,
        topLine: 3,
        source: 'search_everywhere',
        ts: 123,
      }), {
        path: 'src/app.ts',
        name: 'app.ts',
        source: 'search_everywhere',
        ts: 123,
        line: 4,
        column: 2,
        scrollTop: 12,
        topLine: 3,
      });
      assertEqual(createNavigationHistoryEntry({ path: '' }), null);
    },
  },
  {
    name: 'records opens into current and back stack',
    run: () => {
      let state: AgentSpaceNavigationHistoryState = EMPTY_AGENT_SPACE_NAVIGATION_HISTORY;
      state = recordNavigationOpen(state, entry('src/a.ts', 1));
      assertEqual(state.current?.path, 'src/a.ts');
      assertEqual(state.backStack.length, 0);

      state = recordNavigationOpen(state, entry('src/b.ts', 2, 'find_in_files'));
      assertEqual(state.current?.path, 'src/b.ts');
      assertDeepEqual(state.backStack.map((item) => item.path), ['src/a.ts']);
      assertEqual(state.forwardStack.length, 0);
    },
  },
  {
    name: 'deduplicates consecutive same path line and column opens',
    run: () => {
      let state: AgentSpaceNavigationHistoryState = EMPTY_AGENT_SPACE_NAVIGATION_HISTORY;
      state = recordNavigationOpen(state, entry('src/a.ts', 5, 'files'));
      state = recordNavigationOpen(state, entry('src/a.ts', 5, 'find_usages'));
      assertEqual(state.backStack.length, 0);
      assertEqual(state.current?.source, 'find_usages');
      assertEqual(navigationHistoryEntriesEqual(state.current, entry('src/a.ts', 5)), true);
    },
  },
  {
    name: 'navigates back and forward symmetrically',
    run: () => {
      let state: AgentSpaceNavigationHistoryState = EMPTY_AGENT_SPACE_NAVIGATION_HISTORY;
      state = recordNavigationOpen(state, entry('src/a.ts', 1));
      state = recordNavigationOpen(state, entry('src/b.ts', 2));
      state = recordNavigationOpen(state, entry('src/c.ts', 3));

      const back = navigateBack(state, entry('src/c.ts', 3, 'current'));
      assertEqual(back.entry?.path, 'src/b.ts');
      assertDeepEqual(back.state.backStack.map((item) => item.path), ['src/a.ts']);
      assertDeepEqual(back.state.forwardStack.map((item) => item.path), ['src/c.ts']);

      const forward = navigateForward(back.state, entry('src/b.ts', 2, 'current'));
      assertEqual(forward.entry?.path, 'src/c.ts');
      assertDeepEqual(forward.state.backStack.map((item) => item.path), ['src/a.ts', 'src/b.ts']);
      assertEqual(forward.state.forwardStack.length, 0);
    },
  },
  {
    name: 'opening history entries does not add another back item',
    run: () => {
      let state: AgentSpaceNavigationHistoryState = EMPTY_AGENT_SPACE_NAVIGATION_HISTORY;
      state = recordNavigationOpen(state, entry('src/a.ts', 1));
      state = recordNavigationOpen(state, entry('src/b.ts', 2));

      const back = navigateBack(state, entry('src/b.ts', 2));
      const openedFromHistory = recordNavigationOpen(back.state, null);
      assertDeepEqual(openedFromHistory.backStack.map((item) => item.path), []);
      assertDeepEqual(openedFromHistory.forwardStack.map((item) => item.path), ['src/b.ts']);
      assertEqual(openedFromHistory.current?.path, 'src/a.ts');
    },
  },
  {
    name: 'history replay options preserve scroll position without recording history',
    run: () => {
      const replay = navigationHistoryEntryToPreviewReplay({
        path: 'docs/readme.md',
        name: 'readme.md',
        scrollTop: 420,
        topLine: 37,
        source: 'markdown_link',
        ts: 99,
      });
      assertDeepEqual(replay, {
        path: 'docs/readme.md',
        name: 'readme.md',
        options: {
          scrollTop: 420,
          topLine: 37,
          restoreScroll: true,
          recordHistory: false,
          source: 'markdown_link',
        },
      });
    },
  },
  {
    name: 'line-targeted history replay keeps line metadata while restoring scroll',
    run: () => {
      const replay = navigationHistoryEntryToPreviewReplay({
        path: 'src/app.ts',
        name: 'app.ts',
        line: 120,
        column: 9,
        scrollTop: 2400,
        topLine: 101,
        source: 'go_to_definition',
        ts: 100,
      });
      assertDeepEqual(replay, {
        path: 'src/app.ts',
        name: 'app.ts',
        options: {
          line: 120,
          column: 9,
          scrollTop: 2400,
          topLine: 101,
          restoreScroll: true,
          recordHistory: false,
          source: 'go_to_definition',
        },
      });
    },
  },
  {
    name: 'returns empty navigation result when stack is unavailable',
    run: () => {
      const back = navigateBack(EMPTY_AGENT_SPACE_NAVIGATION_HISTORY);
      const forward = navigateForward(EMPTY_AGENT_SPACE_NAVIGATION_HISTORY);
      assertEqual(back.entry, null);
      assertEqual(forward.entry, null);
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
  throw new Error(`${failures} frontend navigation history test(s) failed`);
}

/* SPDX-License-Identifier: Apache-2.0 */
import {
  codeSearchBackendLabel,
  codeSearchOverlayStatusText,
  codeSearchGroups,
  codeSearchResultMetaLabel,
  codeSearchResultPrimaryLabel,
  flattenCodeSearchGroups,
  isCompanionOfflineSearchError,
  moveSearchSelection,
  openCodeSearchResult,
  searchResultFileName,
  shouldRunCodeSearchQuery,
} from '../src/lib/agentSpaceSearch.js';
import type { CodeSearchResult } from '../src/api/codeNavigation.js';

type TestCase = {
  name: string;
  run: () => void | Promise<void>;
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

const tests: TestCase[] = [
  {
    name: 'selection opens preview with result path line and column',
    run: async () => {
      const calls: Array<{ path: string; name: string; target?: { line?: number; column?: number; source?: string } }> = [];
      const opened = await openCodeSearchResult(
        { kind: 'function', path: 'frontend/src/app.tsx', line: 24, column: 7, name: 'AgentSpaceApp' },
        (path, name, target) => {
          calls.push({ path, name, target });
        },
      );

      assertEqual(opened, true);
      assertDeepEqual(calls, [
        {
          path: 'frontend/src/app.tsx',
          name: 'app.tsx',
          target: { line: 24, column: 7, source: 'search_everywhere' },
        },
      ]);
    },
  },
  {
    name: 'selection movement wraps through result bounds',
    run: () => {
      assertEqual(moveSearchSelection(-1, 3, 1), 0);
      assertEqual(moveSearchSelection(-1, 3, -1), 2);
      assertEqual(moveSearchSelection(0, 3, -1), 2);
      assertEqual(moveSearchSelection(2, 3, 1), 0);
      assertEqual(moveSearchSelection(1, 3, 1), 2);
      assertEqual(moveSearchSelection(1, 0, 1), -1);
    },
  },
  {
    name: 'grouping fallback uses first-seen path order',
    run: () => {
      const results: CodeSearchResult[] = [
        { kind: 'text', path: 'src/a.ts', line: 3, column: 1, preview: 'alpha' },
        { kind: 'function', path: 'src/b.ts', line: 9, column: 2, name: 'build' },
        { kind: 'text', path: 'src/a.ts', line: 11, column: 4, preview: 'again' },
      ];

      assertDeepEqual(codeSearchGroups({ groups: [], results }), [
        {
          path: 'src/a.ts',
          results: [
            { kind: 'text', path: 'src/a.ts', line: 3, column: 1, preview: 'alpha' },
            { kind: 'text', path: 'src/a.ts', line: 11, column: 4, preview: 'again' },
          ],
        },
        {
          path: 'src/b.ts',
          results: [{ kind: 'function', path: 'src/b.ts', line: 9, column: 2, name: 'build' }],
        },
      ]);
    },
  },
  {
    name: 'response groups are preferred when present',
    run: () => {
      const grouped = [
        {
          path: 'src/grouped.ts',
          results: [{ kind: 'file' as const, path: 'src/grouped.ts', line: 1, column: 1 }],
        },
      ];

      assertDeepEqual(
        codeSearchGroups({
          groups: grouped,
          results: [{ kind: 'file', path: 'src/flat.ts', line: 1, column: 1 }],
        }),
        grouped,
      );
    },
  },
  {
    name: 'result labels and file names are sensible',
    run: () => {
      assertEqual(searchResultFileName('frontend/src/entries/agent-space.tsx'), 'agent-space.tsx');
      assertEqual(codeSearchResultPrimaryLabel({ kind: 'file', path: 'README.md', line: 1, column: 1 }), 'README.md');
      assertEqual(
        codeSearchResultPrimaryLabel({ kind: 'text', path: 'src/app.ts', line: 7, column: 2, preview: '  create   task  ' }),
        'create task',
      );
      assertEqual(
        codeSearchResultPrimaryLabel({ kind: 'method', path: 'src/app.ts', line: 7, column: 2, name: 'createTask' }),
        'createTask',
      );
      assertEqual(
        codeSearchResultMetaLabel({ kind: 'function', path: 'src/app.ts', line: 7, column: 2, name: 'createTask', container: 'TaskService' }),
        'function in TaskService - src/app.ts:7:2',
      );
    },
  },
  {
    name: 'flattening preserves grouped order',
    run: () => {
      assertDeepEqual(
        flattenCodeSearchGroups([
          { path: 'a.ts', results: [{ kind: 'file', path: 'a.ts', line: 1, column: 1 }] },
          { path: 'b.ts', results: [{ kind: 'text', path: 'b.ts', line: 2, column: 3, preview: 'b' }] },
        ]),
        [
          { kind: 'file', path: 'a.ts', line: 1, column: 1 },
          { kind: 'text', path: 'b.ts', line: 2, column: 3, preview: 'b' },
        ],
      );
    },
  },
  {
    name: 'backend labels map search implementations to user-facing text',
    run: () => {
      assertEqual(codeSearchBackendLabel('rg'), 'Text');
      assertEqual(codeSearchBackendLabel('lsp'), 'Semantic');
      assertEqual(codeSearchBackendLabel('text_fallback'), 'Text fallback');
      assertEqual(codeSearchBackendLabel('symbol_fallback'), 'Symbol fallback');
      assertEqual(codeSearchBackendLabel('definition_fallback'), 'Definition fallback');
      assertEqual(codeSearchBackendLabel('reference_fallback'), 'Reference fallback');
      assertEqual(codeSearchBackendLabel('unknown'), '');
      assertEqual(codeSearchBackendLabel(undefined), '');
    },
  },
  {
    name: 'empty search queries are not runnable',
    run: () => {
      assertEqual(shouldRunCodeSearchQuery(''), false);
      assertEqual(shouldRunCodeSearchQuery('   '), false);
      assertEqual(shouldRunCodeSearchQuery('\n\t'), false);
      assertEqual(shouldRunCodeSearchQuery('task'), true);
    },
  },
  {
    name: 'overlay status waits for completed requests before no results',
    run: () => {
      assertEqual(codeSearchOverlayStatusText({ completed: false, loading: false, resultCount: 0 }), '');
      assertEqual(codeSearchOverlayStatusText({ completed: true, loading: true, resultCount: 0 }), '');
      assertEqual(codeSearchOverlayStatusText({ completed: true, loading: false, resultCount: 0 }), 'No results');
      assertEqual(codeSearchOverlayStatusText({ completed: true, loading: false, resultCount: 3 }), '');
      assertEqual(codeSearchOverlayStatusText({ error: 'Companion offline', completed: true, loading: false, resultCount: 0 }), 'Companion offline');
      assertEqual(codeSearchOverlayStatusText({ status: 'Partial results', completed: true, loading: false, resultCount: 2 }), 'Partial results');
    },
  },
  {
    name: 'offline errors are mapped from companion availability text',
    run: () => {
      assertEqual(isCompanionOfflineSearchError(new Error('Companion unavailable')), true);
      assertEqual(isCompanionOfflineSearchError(new Error('request failed')), false);
    },
  },
];

let failures = 0;
for (const test of tests) {
  try {
    await test.run();
    // eslint-disable-next-line no-console
    console.log(`ok - ${test.name}`);
  } catch (err) {
    failures += 1;
    // eslint-disable-next-line no-console
    console.error(`not ok - ${test.name}`);
    // eslint-disable-next-line no-console
    console.error(err);
  }
}

if (failures) {
  throw new Error(`${failures} frontend AgentSpace search test(s) failed`);
}

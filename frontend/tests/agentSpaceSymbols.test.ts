/* SPDX-License-Identifier: Apache-2.0 */
import {
  openWorkspaceSymbolResult,
  symbolResultMetaLabel,
  symbolResultPreviewName,
  symbolResultPrimaryLabel,
  workspaceSymbolOverlayStatusText,
} from '../src/lib/agentSpaceSymbols.js';

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
    name: 'symbol labels show names while preview names use real files',
    run: () => {
      const result = {
        kind: 'function' as const,
        path: 'src/memos/context/context.py',
        line: 42,
        column: 5,
        name: 'set_request_context',
        container: 'memos.context',
      };

      assertEqual(symbolResultPrimaryLabel(result), 'set_request_context');
      assertEqual(symbolResultPreviewName(result), 'context.py');
      assertEqual(symbolResultMetaLabel(result), 'function in memos.context - src/memos/context/context.py:42:5');
    },
  },
  {
    name: 'opening a symbol uses path line column and go_to_symbol source',
    run: async () => {
      const calls: Array<{ path: string; name: string; target?: { line?: number; column?: number; source?: string } }> = [];
      const opened = await openWorkspaceSymbolResult(
        {
          kind: 'class',
          path: 'frontend/src/entries/agent-space.tsx',
          line: 856,
          column: 10,
          name: 'AgentSpaceApp',
        },
        (path, name, target) => {
          calls.push({ path, name, target });
        },
      );

      assertEqual(opened, true);
      assertDeepEqual(calls, [
        {
          path: 'frontend/src/entries/agent-space.tsx',
          name: 'agent-space.tsx',
          target: { line: 856, column: 10, source: 'go_to_symbol' },
        },
      ]);
      assertEqual(await openWorkspaceSymbolResult(null, () => undefined), false);
    },
  },
  {
    name: 'symbol overlay status handles loading errors partials and empty results',
    run: () => {
      assertEqual(workspaceSymbolOverlayStatusText({ loading: true }), 'Searching symbols...');
      assertEqual(workspaceSymbolOverlayStatusText({ error: 'Companion offline' }), 'Companion offline');
      assertEqual(workspaceSymbolOverlayStatusText({ status: 'Partial results', completed: true, loading: false, resultCount: 2 }), 'Partial results');
      assertEqual(workspaceSymbolOverlayStatusText({ completed: true, loading: false, resultCount: 0 }), 'No symbols');
      assertEqual(workspaceSymbolOverlayStatusText({ completed: true, loading: false, resultCount: 3 }), '');
      assertEqual(workspaceSymbolOverlayStatusText({ completed: false, loading: false, resultCount: 0 }), '');
    },
  },
];

let failures = 0;
for (const test of tests) {
  try {
    await test.run();
    console.log(`ok - ${test.name}`);
  } catch (error) {
    failures += 1;
    console.error(`not ok - ${test.name}`);
    console.error(error instanceof Error ? error.message : String(error));
  }
}

if (failures) {
  throw new Error(`${failures} frontend AgentSpace symbol test(s) failed`);
}

/* SPDX-License-Identifier: Apache-2.0 */
import {
  codeReferenceBackendLabel,
  codeReferenceDrawerTitle,
  codeReferenceNoResultsMessage,
  codeReferenceResultLocationLabel,
  codeReferenceResultPreview,
  flattenCodeReferenceGroups,
  groupCodeReferenceResults,
  openCodeReferenceResult,
} from '../src/lib/agentSpaceReferences.js';
import type { CodeReferenceResult } from '../src/api/codeNavigation.js';

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
    name: 'groups references by first-seen file path',
    run: () => {
      const results: CodeReferenceResult[] = [
        { kind: 'reference', path: 'src/a.ts', line: 3, column: 4, preview: 'runTask()' },
        { kind: 'reference', path: 'src/b.ts', line: 8, column: 2, preview: 'task' },
        { kind: 'reference', path: 'src/a.ts', line: 12, column: 9, preview: 'this.runTask' },
      ];

      assertDeepEqual(groupCodeReferenceResults(results), [
        {
          path: 'src/a.ts',
          results: [
            { kind: 'reference', path: 'src/a.ts', line: 3, column: 4, preview: 'runTask()' },
            { kind: 'reference', path: 'src/a.ts', line: 12, column: 9, preview: 'this.runTask' },
          ],
        },
        {
          path: 'src/b.ts',
          results: [{ kind: 'reference', path: 'src/b.ts', line: 8, column: 2, preview: 'task' }],
        },
      ]);
    },
  },
  {
    name: 'flattens grouped references in render order',
    run: () => {
      assertDeepEqual(
        flattenCodeReferenceGroups([
          { path: 'a.ts', results: [{ kind: 'reference', path: 'a.ts', line: 1, column: 2 }] },
          { path: 'b.ts', results: [{ kind: 'reference', path: 'b.ts', line: 4, column: 5 }] },
        ]),
        [
          { kind: 'reference', path: 'a.ts', line: 1, column: 2 },
          { kind: 'reference', path: 'b.ts', line: 4, column: 5 },
        ],
      );
    },
  },
  {
    name: 'labels fallback references as text matches or possible references',
    run: () => {
      assertEqual(codeReferenceBackendLabel('lsp'), 'Semantic references');
      assertEqual(codeReferenceBackendLabel('reference_fallback'), 'Text matches');
      assertEqual(codeReferenceBackendLabel('rg'), 'Text matches');
      assertEqual(codeReferenceBackendLabel('custom_backend'), 'custom_backend');
      assertEqual(codeReferenceDrawerTitle('lsp'), 'References');
      assertEqual(codeReferenceDrawerTitle('reference_fallback'), 'Possible references');
      assertEqual(codeReferenceNoResultsMessage('lsp'), 'No references found');
      assertEqual(codeReferenceNoResultsMessage('reference_fallback'), 'No text matches found');
      assertEqual(codeReferenceNoResultsMessage('rg'), 'No text matches found');
      assertEqual(codeReferenceNoResultsMessage(undefined), 'No text matches found');
    },
  },
  {
    name: 'formats reference rows with line and preview',
    run: () => {
      assertEqual(
        codeReferenceResultPreview({ kind: 'reference', path: 'src/app.ts', line: 7, column: 11, preview: '  runTask   (task) ' }),
        'runTask (task)',
      );
      assertEqual(codeReferenceResultPreview({ kind: 'reference', path: 'src/app.ts', line: 7, column: 11 }), 'app.ts');
      assertEqual(codeReferenceResultLocationLabel({ kind: 'reference', path: 'src/app.ts', line: 7, column: 11 }), 'L7:11');
      assertEqual(codeReferenceResultLocationLabel({ kind: 'reference', path: 'src/app.ts', line: 0, column: 0 }), '');
    },
  },
  {
    name: 'opens references in preview at result location',
    run: async () => {
      const calls: Array<{ path: string; name: string; target?: { line?: number; column?: number; source?: string } }> = [];
      const opened = await openCodeReferenceResult(
        { kind: 'reference', path: 'frontend/src/app.tsx', line: 24, column: 7, preview: 'AgentSpaceApp' },
        (path, name, target) => {
          calls.push({ path, name, target });
        },
      );

      assertEqual(opened, true);
      assertDeepEqual(calls, [
        {
          path: 'frontend/src/app.tsx',
          name: 'app.tsx',
          target: { line: 24, column: 7, source: 'find_usages' },
        },
      ]);
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
  throw new Error(`${failures} frontend AgentSpace references test(s) failed`);
}

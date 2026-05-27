/* SPDX-License-Identifier: Apache-2.0 */
import {
  definitionResultMetaLabel,
  definitionResultPreviewName,
  definitionResultPrimaryLabel,
} from '../src/lib/agentSpaceDefinitionResults.js';

type TestCase = {
  name: string;
  run: () => void;
};

function assertEqual<T>(actual: T, expected: T, message?: string): void {
  if (actual !== expected) {
    throw new Error(`${message ? `${message}: ` : ''}expected ${String(expected)}, got ${String(actual)}`);
  }
}

const tests: TestCase[] = [
  {
    name: 'definition list labels keep symbol names while preview names use real files',
    run: () => {
      const result = {
        kind: 'function' as const,
        path: 'src/memos/context/context.py',
        line: 42,
        column: 5,
        name: 'set_request_context',
        container: 'memos.context',
      };

      assertEqual(definitionResultPrimaryLabel(result), 'set_request_context');
      assertEqual(definitionResultPreviewName(result), 'context.py');
      assertEqual(definitionResultMetaLabel(result), 'function in memos.context - src/memos/context/context.py:42:5');
    },
  },
  {
    name: 'definition labels fall back to file name when symbol name is absent',
    run: () => {
      const result = {
        kind: 'method' as const,
        path: 'frontend/src/entries/agent-space.tsx',
        line: 1,
        column: 1,
        name: '',
      };

      assertEqual(definitionResultPrimaryLabel(result), 'agent-space.tsx');
      assertEqual(definitionResultPreviewName(result), 'agent-space.tsx');
    },
  },
];

let failures = 0;
for (const test of tests) {
  try {
    test.run();
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
  throw new Error(`${failures} frontend AgentSpace definition result test(s) failed`);
}

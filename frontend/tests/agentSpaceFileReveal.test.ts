/* SPDX-License-Identifier: Apache-2.0 */
import {
  agentSpaceFileAncestorPaths,
  createAgentSpaceFileRevealPlan,
  normalizeAgentSpaceFilePath,
} from '../src/lib/agentSpaceFileReveal.js';

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

const tests: TestCase[] = [
  {
    name: 'normalizes workspace-relative paths for file tree matching',
    run: () => {
      assertEqual(normalizeAgentSpaceFilePath(' src/app.ts '), 'src/app.ts');
      assertEqual(normalizeAgentSpaceFilePath('./src//lib\\app.ts'), 'src/lib/app.ts');
      assertEqual(normalizeAgentSpaceFilePath('/src/app.ts'), 'src/app.ts');
      assertEqual(normalizeAgentSpaceFilePath('../outside.ts'), '');
    },
  },
  {
    name: 'returns ancestor directories from workspace root to parent directory',
    run: () => {
      assertDeepEqual(agentSpaceFileAncestorPaths('README.md'), ['']);
      assertDeepEqual(agentSpaceFileAncestorPaths('src/lib/app.ts'), ['', 'src', 'src/lib']);
      assertDeepEqual(agentSpaceFileAncestorPaths(''), []);
    },
  },
  {
    name: 'creates a reveal plan for valid file paths',
    run: () => {
      assertDeepEqual(createAgentSpaceFileRevealPlan('src/lib/app.ts'), {
        targetPath: 'src/lib/app.ts',
        ancestorPaths: ['', 'src', 'src/lib'],
      });
      assertEqual(createAgentSpaceFileRevealPlan(''), null);
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
  throw new Error(`${failures} frontend AgentSpace file reveal test(s) failed`);
}

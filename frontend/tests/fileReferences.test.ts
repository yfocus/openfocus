/* SPDX-License-Identifier: Apache-2.0 */
import {
  fileReferenceFromTerminalMessage,
  parseSingleFileReference,
  parseTerminalFileReference,
  workspaceRelativePath,
} from '../src/lib/fileReferences.js';

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

function assertNull(actual: unknown, message?: string): void {
  if (actual !== null) throw new Error(`${message ? `${message}: ` : ''}expected null, got ${JSON.stringify(actual)}`);
}

const tests: TestCase[] = [
  {
    name: 'parses agent-style path and line anchor',
    run: () => {
      assertDeepEqual(parseSingleFileReference('@src/app.py#L10'), { path: 'src/app.py', line: 10 });
    },
  },
  {
    name: 'parses terminal path line and column suffix',
    run: () => {
      assertDeepEqual(parseTerminalFileReference('src/app.py:12:4'), { path: 'src/app.py', line: 12, column: 4 });
    },
  },
  {
    name: 'maps file URL inside workspace root to relative path',
    run: () => {
      const reference = fileReferenceFromTerminalMessage(
        { href: 'file:///Users/me/repo/src/app.py#L3' },
        '/Users/me/repo',
      );
      assertEqual(reference?.relPath, 'src/app.py');
    },
  },
  {
    name: 'accepts absolute paths inside workspace root and rejects outside paths',
    run: () => {
      assertEqual(workspaceRelativePath('/Users/me/repo/src/app.py', '/Users/me/repo'), 'src/app.py');
      assertNull(workspaceRelativePath('/Users/me/other/src/app.py', '/Users/me/repo'));
    },
  },
  {
    name: 'rejects path traversal that escapes relative workspace path',
    run: () => {
      assertNull(workspaceRelativePath('../secret.txt', '/Users/me/repo'));
    },
  },
  {
    name: 'ignores http and https URLs as file references',
    run: () => {
      assertNull(parseSingleFileReference('https://example.com/src/app.py#L10'));
      assertNull(parseSingleFileReference('http://example.com/src/app.py:10'));
    },
  },
  {
    name: 'terminal messages try path href and text in current priority order',
    run: () => {
      assertDeepEqual(
        fileReferenceFromTerminalMessage(
          { path: '/Users/me/other/app.py', href: 'src/from-href.py:4', text: 'src/from-text.py:7', line: '9' },
          '/Users/me/repo',
        ),
        { relPath: 'src/from-href.py', line: 9 },
      );
      assertDeepEqual(
        fileReferenceFromTerminalMessage(
          { href: 'https://example.com/docs', text: 'See @src/from-text.py#L7' },
          '/Users/me/repo',
        ),
        { relPath: 'src/from-text.py', line: 7 },
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
  throw new Error(`${failures} frontend file reference test(s) failed`);
}

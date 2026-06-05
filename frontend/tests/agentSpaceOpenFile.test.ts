/* SPDX-License-Identifier: Apache-2.0 */
import {
  filterOpenFileEntries,
  normalizeOpenFileEntries,
  openAgentSpaceFileMatch,
  openFileNameFromPath,
  openFileOverlayStatusText,
} from '../src/lib/agentSpaceOpenFile.js';

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
    name: 'normalizes files response and path fallback into unique relative entries',
    run: () => {
      assertDeepEqual(
        normalizeOpenFileEntries(
          [
            { path: './src/app.tsx', name: '', size: 1024, mtime: 123 },
            { path: 'src/app.tsx', name: 'duplicate.tsx' },
            { path: '../outside.ts' },
            { path: 'README.md', name: 'Readme', size: -1 },
          ],
          ['docs/intro.md', 'src/app.tsx'],
        ),
        [
          { path: 'src/app.tsx', name: 'app.tsx', size: 1024, mtime: 123 },
          { path: 'README.md', name: 'Readme' },
          { path: 'docs/intro.md', name: 'intro.md' },
        ],
      );
      assertEqual(openFileNameFromPath('/src/lib/open.ts'), 'open.ts');
      assertEqual(openFileNameFromPath(''), 'Untitled');
    },
  },
  {
    name: 'filters and ranks fuzzy path matches',
    run: () => {
      const files = normalizeOpenFileEntries([
        { path: 'frontend/src/entries/agent-space.tsx', name: 'agent-space.tsx' },
        { path: 'frontend/src/lib/agentSpaceOpenFile.ts', name: 'agentSpaceOpenFile.ts' },
        { path: 'README.md', name: 'README.md' },
        { path: 'openfocus/web/routes/agent_spaces.py', name: 'agent_spaces.py' },
      ]);

      assertEqual(filterOpenFileEntries(files, 'agent open')[0]?.file.path, 'frontend/src/lib/agentSpaceOpenFile.ts');
      assertDeepEqual(filterOpenFileEntries(files, 'agent openfile').map((match) => match.file.path), [
        'frontend/src/lib/agentSpaceOpenFile.ts',
      ]);
      assertEqual(filterOpenFileEntries(files, 'asp')[0]?.file.path, 'frontend/src/entries/agent-space.tsx');
      assertEqual(filterOpenFileEntries(files, 'read')[0]?.file.path, 'README.md');
      assertDeepEqual(filterOpenFileEntries(files, '').map((match) => match.file.path), files.map((file) => file.path));
    },
  },
  {
    name: 'formats overlay status from cache and result state',
    run: () => {
      assertEqual(openFileOverlayStatusText({ loading: true }), 'Loading files...');
      assertEqual(openFileOverlayStatusText({ error: 'Companion offline' }), 'Companion offline');
      assertEqual(openFileOverlayStatusText({ filesLoaded: true, resultCount: 0, total: 3, cacheHit: true }), 'No matching files');
      assertEqual(openFileOverlayStatusText({ filesLoaded: true, resultCount: 0, total: 0, cacheHit: true }), 'No files');
      assertEqual(openFileOverlayStatusText({ filesLoaded: true, resultCount: 2, total: 2, cacheHit: true }), '2 files (cached)');
      assertEqual(openFileOverlayStatusText({ filesLoaded: true, resultCount: 1, total: 1, cacheHit: false, truncated: true }), '1 file (fresh), partial');
    },
  },
  {
    name: 'opens selected file using real path name and open_file source',
    run: async () => {
      const calls: Array<{ path: string; name: string; target?: { source?: string } }> = [];
      const opened = await openAgentSpaceFileMatch(
        { file: { path: 'src/actual.ts', name: 'actual.ts' }, score: 1 },
        (path, name, target) => {
          calls.push({ path, name, target });
        },
      );

      assertEqual(opened, true);
      assertDeepEqual(calls, [{ path: 'src/actual.ts', name: 'actual.ts', target: { source: 'open_file' } }]);
      assertEqual(await openAgentSpaceFileMatch(null, () => undefined), false);
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
  throw new Error(`${failures} frontend AgentSpace Open File test(s) failed`);
}

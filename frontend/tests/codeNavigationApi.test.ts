/* SPDX-License-Identifier: Apache-2.0 */
import {
  findCodeDefinition,
  findCodeReferences,
  getCodeNavigationStatus,
  getCodeSymbols,
  groupCodeSearchResults,
  searchCode,
} from '../src/api/codeNavigation.js';
import type { CodeSearchResult } from '../src/api/codeNavigation.js';

type TestCase = {
  name: string;
  run: () => void | Promise<void>;
};

type FetchCall = {
  url: string;
  options?: RequestInit;
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

function parseRequestUrl(rawUrl: string): URL {
  return new URL(rawUrl, 'http://openfocus.test');
}

function installFetch(payload: unknown): FetchCall[] {
  const calls: FetchCall[] = [];
  const fetchFake = async (url: RequestInfo | URL, options?: RequestInit): Promise<Response> => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    } as Response;
  };
  Object.defineProperty(globalThis, 'fetch', { configurable: true, value: fetchFake });
  return calls;
}

const tests: TestCase[] = [
  {
    name: 'searchCode builds encoded search query and omits empty optional filters',
    run: async () => {
      const payload = {
        ok: true,
        query: 'create space',
        kind: 'text',
        backend: 'text_fallback',
        truncated: false,
        results: [
          { kind: 'text', path: 'src/app.ts', line: 12, column: 8, preview: 'create space', backend: 'rg' },
          { kind: 'function', path: 'src/actions.ts', line: 4, column: 17, name: 'createSpace', backend: 'rg' },
          { kind: 'text', path: 'src/app.ts', line: 20, column: 12, preview: 'create space task', backend: 'rg' },
        ],
        groups: [
          {
            path: 'src/app.ts',
            results: [
              { kind: 'text', path: 'src/app.ts', line: 12, column: 8, preview: 'create space', backend: 'rg' },
              { kind: 'text', path: 'src/app.ts', line: 20, column: 12, preview: 'create space task', backend: 'rg' },
            ],
          },
          {
            path: 'src/actions.ts',
            results: [
              { kind: 'function', path: 'src/actions.ts', line: 4, column: 17, name: 'createSpace', backend: 'rg' },
            ],
          },
        ],
      };
      const calls = installFetch(payload);

      const result = await searchCode(42, {
        q: 'create space',
        kind: 'text',
        include: 'src/**/*.ts',
        exclude: '',
        regex: true,
        caseSensitive: false,
        limit: 25,
      });

      assertDeepEqual(result, payload);
      assertEqual(calls.length, 1);
      const requestUrl = parseRequestUrl(calls[0].url);
      assertEqual(requestUrl.pathname, '/api/agent_spaces/42/code/search');
      assertEqual(requestUrl.searchParams.get('q'), 'create space');
      assertEqual(requestUrl.searchParams.get('kind'), 'text');
      assertEqual(requestUrl.searchParams.get('include'), 'src/**/*.ts');
      assertEqual(requestUrl.searchParams.get('exclude'), null);
      assertEqual(requestUrl.searchParams.get('case_sensitive'), null);
      assertEqual(requestUrl.searchParams.get('regex'), 'true');
      assertEqual(requestUrl.searchParams.get('limit'), '25');
      assertDeepEqual(calls[0].options, {});
    },
  },
  {
    name: 'getCodeSymbols sends only spec-supported symbol filters',
    run: async () => {
      const payload = {
        ok: true,
        query: 'build',
        backend: 'symbol_fallback',
        truncated: false,
        results: [
          {
            kind: 'function',
            name: 'buildReport',
            container: '',
            path: 'src/report.ts',
            line: 4,
            column: 17,
            backend: 'symbol_fallback',
          },
        ],
      };
      const calls = installFetch(payload);

      const result = await getCodeSymbols(7, { q: 'build', limit: 10 });

      assertDeepEqual(result, payload);
      const requestUrl = parseRequestUrl(calls[0].url);
      assertEqual(requestUrl.pathname, '/api/agent_spaces/7/code/symbols');
      assertEqual(requestUrl.searchParams.get('q'), 'build');
      assertEqual(requestUrl.searchParams.get('include'), null);
      assertEqual(requestUrl.searchParams.get('exclude'), null);
      assertEqual(requestUrl.searchParams.get('limit'), '10');
    },
  },
  {
    name: 'groupCodeSearchResults groups flat search results by first-seen file path',
    run: () => {
      const results: CodeSearchResult[] = [
        { kind: 'text', path: 'src/app.ts', line: 2, column: 4, preview: 'buildReport()' },
        { kind: 'function', path: 'src/report.ts', line: 8, column: 17, name: 'buildReport' },
        { kind: 'text', path: 'src/app.ts', line: 20, column: 6, preview: 'buildReport(input)' },
      ];

      assertDeepEqual(groupCodeSearchResults(results), [
        {
          path: 'src/app.ts',
          results: [
            { kind: 'text', path: 'src/app.ts', line: 2, column: 4, preview: 'buildReport()' },
            { kind: 'text', path: 'src/app.ts', line: 20, column: 6, preview: 'buildReport(input)' },
          ],
        },
        {
          path: 'src/report.ts',
          results: [{ kind: 'function', path: 'src/report.ts', line: 8, column: 17, name: 'buildReport' }],
        },
      ]);
    },
  },
  {
    name: 'groupCodeSearchResults returns no groups for an empty search result set',
    run: () => {
      assertDeepEqual(groupCodeSearchResults([]), []);
    },
  },
  {
    name: 'findCodeDefinition posts JSON location payload',
    run: async () => {
      const payload = {
        ok: true,
        symbol: 'buildReport',
        backend: 'definition_fallback',
        truncated: false,
        results: [{ kind: 'function', name: 'buildReport', path: 'src/report.ts', line: 8, column: 17 }],
      };
      const calls = installFetch(payload);

      const result = await findCodeDefinition(9, { path: 'src/app.ts', line: 3, column: 14, symbol: 'buildReport' });

      assertDeepEqual(result, payload);
      assertEqual(calls[0].url, '/api/agent_spaces/9/code/definition');
      assertEqual(calls[0].options?.method, 'POST');
      assertDeepEqual(calls[0].options?.headers, { 'Content-Type': 'application/json' });
      assertEqual(calls[0].options?.body, JSON.stringify({ path: 'src/app.ts', line: 3, column: 14, symbol: 'buildReport' }));
    },
  },
  {
    name: 'findCodeReferences posts JSON location payload',
    run: async () => {
      const payload = {
        ok: true,
        symbol: 'buildReport',
        backend: 'reference_fallback',
        truncated: false,
        results: [{ kind: 'reference', path: 'src/app.ts', line: 3, column: 14, preview: 'buildReport()' }],
      };
      const calls = installFetch(payload);

      const result = await findCodeReferences(9, { path: 'src/app.ts', line: 3, column: 14, symbol: 'buildReport' });

      assertDeepEqual(result, payload);
      assertEqual(calls[0].url, '/api/agent_spaces/9/code/references');
      assertEqual(calls[0].options?.method, 'POST');
      assertDeepEqual(calls[0].options?.headers, { 'Content-Type': 'application/json' });
      assertEqual(calls[0].options?.body, JSON.stringify({ path: 'src/app.ts', line: 3, column: 14, symbol: 'buildReport' }));
    },
  },
  {
    name: 'getCodeNavigationStatus fetches status without query parameters',
    run: async () => {
      const payload = {
        ok: true,
        backend: 'text_fallback',
        ripgrep_available: true,
        lsp_available: false,
        active_language_servers: [],
        fallback_mode: true,
      };
      const calls = installFetch(payload);

      const result = await getCodeNavigationStatus(4);

      assertDeepEqual(result, payload);
      assertEqual(calls[0].url, '/api/agent_spaces/4/code/status');
      assertDeepEqual(calls[0].options, {});
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
  throw new Error(`${failures} frontend code navigation API test(s) failed`);
}

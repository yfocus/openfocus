/* SPDX-License-Identifier: Apache-2.0 */
import {
  createPreviewSymbolContext,
  navigationSymbolFromText,
  wordAtCursor,
} from '../src/lib/agentSpacePreviewSymbol.js';

type TestCase = {
  name: string;
  run: () => void | Promise<void>;
};

function assertEqual<T>(actual: T, expected: T, message?: string): void {
  if (actual !== expected) {
    throw new Error(`${message ? `${message}: ` : ''}expected ${String(expected)}, got ${String(actual)}`);
  }
}

const tests: TestCase[] = [
  {
    name: 'cursor inside identifier exposes wordAtCursor as navigation symbol',
    run: () => {
      const content = 'const fooBar = buildReport();';
      const context = createPreviewSymbolContext({
        path: 'src/app.ts',
        content,
        selection: { from: 8, to: 8, head: 8 },
      });

      assertEqual(context.relPath, 'src/app.ts');
      assertEqual(context.path, 'src/app.ts');
      assertEqual(context.line, 1);
      assertEqual(context.column, 7);
      assertEqual(context.selectedText, '');
      assertEqual(context.wordAtCursor, 'fooBar');
      assertEqual(context.symbol, 'fooBar');
    },
  },
  {
    name: 'valid selected symbol is preferred over wordAtCursor',
    run: () => {
      const content = 'const fooBar = buildReport();';
      const context = createPreviewSymbolContext({
        path: 'src/app.ts',
        content,
        selection: { from: 15, to: 26, head: 26 },
      });

      assertEqual(context.selectedText, 'buildReport');
      assertEqual(context.wordAtCursor, '');
      assertEqual(context.symbol, 'buildReport');
      assertEqual(context.column, 16);
    },
  },
  {
    name: 'invalid selection falls back to wordAtCursor',
    run: () => {
      const content = 'const fooBar = buildReport();';
      const context = createPreviewSymbolContext({
        path: 'src/app.ts',
        content,
        selection: { from: 6, to: 13, head: 10, selectedText: 'foo-bar' },
      });

      assertEqual(context.selectedText, 'foo-bar');
      assertEqual(context.wordAtCursor, 'fooBar');
      assertEqual(context.symbol, 'fooBar');
    },
  },
  {
    name: 'blank whitespace and punctuation do not produce navigation symbols',
    run: () => {
      const blank = createPreviewSymbolContext({
        path: 'src/app.ts',
        content: 'const foo = 1;',
        selection: { from: 5, to: 6, head: 6 },
      });
      const punctuation = createPreviewSymbolContext({
        path: 'src/app.ts',
        content: 'foo();',
        selection: { from: 3, to: 3, head: 3 },
      });

      assertEqual(blank.selectedText, ' ');
      assertEqual(blank.symbol, '');
      assertEqual(punctuation.wordAtCursor, '');
      assertEqual(punctuation.symbol, '');
      assertEqual(navigationSymbolFromText('!!!'), '');
    },
  },
  {
    name: 'overlong strings do not produce navigation symbols',
    run: () => {
      const longSymbol = `a${'b'.repeat(200)}`;
      const context = createPreviewSymbolContext({
        path: 'src/app.ts',
        content: longSymbol,
        selection: { from: 0, to: longSymbol.length, head: longSymbol.length },
      });

      assertEqual(context.selectedText, longSymbol);
      assertEqual(context.symbol, '');
      assertEqual(navigationSymbolFromText(longSymbol), '');
    },
  },
  {
    name: 'wordAtCursor supports cursor at end of identifier',
    run: () => {
      const word = wordAtCursor('return fooBar', 13);

      assertEqual(word.text, 'fooBar');
      assertEqual(word.from, 7);
      assertEqual(word.to, 13);
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
  throw new Error(`${failures} frontend AgentSpace preview symbol test(s) failed`);
}

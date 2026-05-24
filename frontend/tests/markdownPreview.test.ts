/* SPDX-License-Identifier: Apache-2.0 */
import {
  isMarkdownPreviewFile,
  renderMarkdownToHtml,
  shouldRenderMarkdownPreview,
} from '../src/lib/markdownPreview.js';

type TestCase = {
  name: string;
  run: () => void;
};

function assertEqual<T>(actual: T, expected: T, message?: string): void {
  if (actual !== expected) {
    throw new Error(`${message ? `${message}: ` : ''}expected ${String(expected)}, got ${String(actual)}`);
  }
}

function assertIncludes(actual: string, expected: string, message?: string): void {
  if (!actual.includes(expected)) {
    throw new Error(`${message ? `${message}: ` : ''}expected ${JSON.stringify(actual)} to include ${JSON.stringify(expected)}`);
  }
}

function assertNotIncludes(actual: string, expected: string, message?: string): void {
  if (actual.includes(expected)) {
    throw new Error(`${message ? `${message}: ` : ''}expected ${JSON.stringify(actual)} not to include ${JSON.stringify(expected)}`);
  }
}

const tests: TestCase[] = [
  {
    name: 'markdown files render by default unless source mode is selected',
    run: () => {
      assertEqual(isMarkdownPreviewFile('README.md'), true);
      assertEqual(isMarkdownPreviewFile('notes.markdown'), true);
      assertEqual(isMarkdownPreviewFile('app.ts'), false);
      assertEqual(shouldRenderMarkdownPreview('README.md', false), true);
      assertEqual(shouldRenderMarkdownPreview('README.md', true), false);
      assertEqual(shouldRenderMarkdownPreview('app.ts', false), false);
    },
  },
  {
    name: 'markdown renderer emits safe document html',
    run: () => {
      const html = renderMarkdownToHtml('# Title\n\nHello **focus** and `code`.\n\n<script>alert(1)</script>');

      assertIncludes(html, '<h1>Title</h1>');
      assertIncludes(html, '<strong>focus</strong>');
      assertIncludes(html, '<code>code</code>');
      assertIncludes(html, '&lt;script&gt;alert(1)&lt;/script&gt;');
      assertNotIncludes(html, '<script>');
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
  throw new Error(`${failures} frontend Markdown preview test(s) failed`);
}

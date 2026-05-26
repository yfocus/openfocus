/* SPDX-License-Identifier: Apache-2.0 */
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  isMarkdownPreviewFile,
  MarkdownPreview,
  resolveMarkdownUrl,
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
      const html = renderToStaticMarkup(React.createElement(MarkdownPreview, {
        content: '# Title\n\nHello **focus** and `code`.\n\n<script>alert(1)</script>',
        fontSize: 12,
        path: 'README.md',
      }));

      assertIncludes(html, '<h1>Title</h1>');
      assertIncludes(html, '<strong>focus</strong>');
      assertIncludes(html, '<code>code</code>');
      assertIncludes(html, '&lt;script&gt;alert(1)&lt;/script&gt;');
      assertNotIncludes(html, '<script>');
    },
  },
  {
    name: 'markdown renderer supports GFM tables',
    run: () => {
      const html = renderToStaticMarkup(React.createElement(MarkdownPreview, {
        content: '| Name | Status |\n| --- | --- |\n| Preview | Ready |',
        fontSize: 12,
        path: 'docs/readme.md',
      }));

      assertIncludes(html, '<table>');
      assertIncludes(html, '<th>Name</th>');
      assertIncludes(html, '<td>Ready</td>');
    },
  },
  {
    name: 'markdown renderer maps workspace images through raw file URLs',
    run: () => {
      const html = renderToStaticMarkup(React.createElement(MarkdownPreview, {
        content: '![Diagram](./assets/diagram.png)',
        fontSize: 12,
        path: 'docs/readme.md',
        imageSrcForPath: (path) => `/raw?path=${encodeURIComponent(path)}`,
      }));

      assertIncludes(html, '<img');
      assertIncludes(html, 'alt="Diagram"');
      assertIncludes(html, 'src="/raw?path=docs%2Fassets%2Fdiagram.png"');
    },
  },
  {
    name: 'markdown image renderer does not emit non-image external protocols',
    run: () => {
      const html = renderToStaticMarkup(React.createElement(MarkdownPreview, {
        content: '![Bad](mailto:test@example.com)',
        fontSize: 12,
        path: 'docs/readme.md',
      }));

      assertNotIncludes(html, '<img');
      assertNotIncludes(html, 'src="mailto:test@example.com"');
    },
  },
  {
    name: 'markdown renderer sanitizes common README raw html instead of showing literal tags',
    run: () => {
      const html = renderToStaticMarkup(React.createElement(MarkdownPreview, {
        content: '<div align="center"> <a href="https://memos.openmem.net/"> <img src="https://statics.memtensor.com.cn/memos/memos-banner.gif" alt="MemOS Banner"> </a>\n\n<h1 align="center"> <img src="https://statics.memtensor.com.cn/logo/memos_color_m.png" alt="MemOS Logo" width="50"/> MemOS 2.0 <img src="https://img.shields.io/badge/status-Preview-blue" alt="Preview Badge"/> </h1>\n\n<p align="center"> <strong>Accuracy</strong><br/> <sub>LoCoMo</sub> <script>alert(1)</script> <img src="javascript:alert(1)" onerror="alert(1)" alt="Bad"/> </p>\n\n</div>',
        fontSize: 12,
        path: 'README.md',
      }));

      assertIncludes(html, '<div align="center">');
      assertIncludes(html, '<a href="https://memos.openmem.net/"');
      assertIncludes(html, 'src="https://statics.memtensor.com.cn/memos/memos-banner.gif"');
      assertIncludes(html, '<h1 align="center">');
      assertIncludes(html, 'width="50"');
      assertIncludes(html, '<br/>');
      assertIncludes(html, '<sub>LoCoMo</sub>');
      assertNotIncludes(html, '&lt;div');
      assertNotIncludes(html, '<script>');
      assertNotIncludes(html, 'javascript:alert');
      assertNotIncludes(html, 'onerror');
    },
  },
  {
    name: 'markdown renderer keeps inline raw html children inside sanitized tags',
    run: () => {
      const html = renderToStaticMarkup(React.createElement(MarkdownPreview, {
        content: 'Read <a href="https://example.com" onclick="alert(1)"><img src="https://example.com/badge.svg" alt="Badge"> docs</a> now.',
        fontSize: 12,
        path: 'README.md',
      }));

      assertIncludes(html, '<a href="https://example.com" target="_blank" rel="noreferrer"><img src="https://example.com/badge.svg" alt="Badge"/> docs</a>');
      assertNotIncludes(html, '<a href="https://example.com" target="_blank" rel="noreferrer"></a>');
      assertNotIncludes(html, 'onclick');
    },
  },
  {
    name: 'markdown links distinguish safe external, workspace, and unsafe hrefs',
    run: () => {
      assertEqual(resolveMarkdownUrl('https://example.com/docs', 'docs/readme.md').kind, 'external');
      assertEqual(resolveMarkdownUrl('./guide.md', 'docs/readme.md').path, 'docs/guide.md');
      assertEqual(resolveMarkdownUrl('../guide.md', 'docs/nested/readme.md').path, 'docs/guide.md');
      assertEqual(resolveMarkdownUrl('../../escape.md', 'docs/readme.md').kind, 'unsafe');

      const html = renderToStaticMarkup(React.createElement(MarkdownPreview, {
        content: '[Guide](./guide.md) [Bad](javascript:alert(1))',
        fontSize: 12,
        path: 'docs/readme.md',
      }));

      assertIncludes(html, 'data-workspace-path="docs/guide.md"');
      assertNotIncludes(html, 'href="javascript:alert(1)"');
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

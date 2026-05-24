/* SPDX-License-Identifier: Apache-2.0 */

const MARKDOWN_FILE_RE = /\.(md|markdown)$/i;

type ListKind = 'ul' | 'ol';

function cleanFileName(name: string): string {
  return String(name || '').split(/[?#]/, 1)[0] || '';
}

export function isMarkdownPreviewFile(name: string): boolean {
  return MARKDOWN_FILE_RE.test(cleanFileName(name));
}

export function shouldRenderMarkdownPreview(name: string, sourceMode: boolean): boolean {
  return isMarkdownPreviewFile(name) && !sourceMode;
}

function escapeHtml(value: string): string {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function safeHref(rawHref: string): string {
  const href = String(rawHref || '').trim();
  if (!href) return '';
  if (/^(https?:|mailto:|#)/i.test(href)) return href;
  return '';
}

function renderEmphasis(escaped: string): string {
  return escaped
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_]+)__/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/_([^_]+)_/g, '<em>$1</em>');
}

function renderInlinePlain(text: string): string {
  const linkPattern = /\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
  let html = '';
  let offset = 0;
  for (const match of text.matchAll(linkPattern)) {
    const index = match.index || 0;
    html += renderEmphasis(escapeHtml(text.slice(offset, index)));
    const href = safeHref(match[2] || '');
    if (href) {
      html += '<a href="' + escapeHtml(href) + '" target="_blank" rel="noreferrer">' + renderEmphasis(escapeHtml(match[1] || '')) + '</a>';
    } else {
      html += renderEmphasis(escapeHtml(match[0] || ''));
    }
    offset = index + (match[0] || '').length;
  }
  html += renderEmphasis(escapeHtml(text.slice(offset)));
  return html;
}

function renderInline(text: string): string {
  const parts = String(text || '').split(/(`[^`]*`)/g);
  return parts
    .map((part) => {
      if (part.startsWith('`') && part.endsWith('`') && part.length >= 2) {
        return '<code>' + escapeHtml(part.slice(1, -1)) + '</code>';
      }
      return renderInlinePlain(part);
    })
    .join('');
}

function isFenceStart(line: string): RegExpMatchArray | null {
  return line.match(/^ {0,3}(```|~~~)(.*)$/);
}

function isBlank(line: string): boolean {
  return /^\s*$/.test(line);
}

function isHeading(line: string): RegExpMatchArray | null {
  return line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*$/);
}

function isHorizontalRule(line: string): boolean {
  return /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line);
}

function listMatch(line: string): { kind: ListKind; text: string } | null {
  const unordered = line.match(/^ {0,3}[-*+]\s+(.+)$/);
  if (unordered) return { kind: 'ul', text: unordered[1] || '' };
  const ordered = line.match(/^ {0,3}\d+[.)]\s+(.+)$/);
  if (ordered) return { kind: 'ol', text: ordered[1] || '' };
  return null;
}

function isBlockStart(line: string): boolean {
  return isBlank(line) || !!isFenceStart(line) || !!isHeading(line) || isHorizontalRule(line) || /^ {0,3}>/.test(line) || !!listMatch(line);
}

function renderList(lines: string[], start: number, kind: ListKind): { html: string; next: number } {
  const items: string[] = [];
  let index = start;
  while (index < lines.length) {
    const match = listMatch(lines[index] || '');
    if (!match || match.kind !== kind) break;
    items.push('<li>' + renderInline(match.text.trim()) + '</li>');
    index += 1;
  }
  return { html: '<' + kind + '>' + items.join('') + '</' + kind + '>', next: index };
}

function renderBlockquote(lines: string[], start: number): { html: string; next: number } {
  const quoted: string[] = [];
  let index = start;
  while (index < lines.length) {
    const line = lines[index] || '';
    if (!/^ {0,3}>/.test(line)) break;
    quoted.push(line.replace(/^ {0,3}> ?/, ''));
    index += 1;
  }
  return { html: '<blockquote>' + renderMarkdownToHtml(quoted.join('\n')) + '</blockquote>', next: index };
}

function renderParagraph(lines: string[], start: number): { html: string; next: number } {
  const paragraph: string[] = [];
  let index = start;
  while (index < lines.length && !isBlockStart(lines[index] || '')) {
    paragraph.push((lines[index] || '').trim());
    index += 1;
  }
  return { html: '<p>' + renderInline(paragraph.join(' ')) + '</p>', next: index };
}

export function renderMarkdownToHtml(source: string): string {
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
  const blocks: string[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index] || '';
    if (isBlank(line)) {
      index += 1;
      continue;
    }

    const fence = isFenceStart(line);
    if (fence) {
      const marker = fence[1] || '```';
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !(lines[index] || '').startsWith(marker)) {
        code.push(lines[index] || '');
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push('<pre><code>' + escapeHtml(code.join('\n')) + '</code></pre>');
      continue;
    }

    const heading = isHeading(line);
    if (heading) {
      const level = Math.min(6, Math.max(1, (heading[1] || '#').length));
      const text = (heading[2] || '').replace(/\s+#+\s*$/, '');
      blocks.push('<h' + level + '>' + renderInline(text) + '</h' + level + '>');
      index += 1;
      continue;
    }

    if (isHorizontalRule(line)) {
      blocks.push('<hr>');
      index += 1;
      continue;
    }

    if (/^ {0,3}>/.test(line)) {
      const rendered = renderBlockquote(lines, index);
      blocks.push(rendered.html);
      index = rendered.next;
      continue;
    }

    const list = listMatch(line);
    if (list) {
      const rendered = renderList(lines, index, list.kind);
      blocks.push(rendered.html);
      index = rendered.next;
      continue;
    }

    const paragraph = renderParagraph(lines, index);
    blocks.push(paragraph.html);
    index = paragraph.next;
  }

  return blocks.join('\n');
}

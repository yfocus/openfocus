/* SPDX-License-Identifier: Apache-2.0 */
import React, { type CSSProperties, type ReactElement, type ReactNode } from 'react';
import { parser as htmlParser } from '@lezer/html';
import { GFM, parser as markdownParser } from '@lezer/markdown';

const MARKDOWN_FILE_RE = /\.(md|markdown)$/i;
const markdownGfmParser = markdownParser.configure([GFM]);

type ParsedMarkdownNode = {
  name: string;
  from: number;
  to: number;
  children: ParsedMarkdownNode[];
};

type ParsedHtmlNode = ParsedMarkdownNode;

export type MarkdownUrlResolution =
  | { kind: 'external'; href: string; path?: undefined }
  | { kind: 'anchor'; href: string; path?: undefined }
  | { kind: 'workspace'; href: string; path: string }
  | { kind: 'unsafe'; href: ''; path?: undefined };

export type MarkdownPreviewProps = {
  content: string;
  fontSize: number;
  path?: string;
  imageSrcForPath?: (path: string) => string;
  onOpenWorkspacePath?: (path: string) => void;
};

function cleanFileName(name: string): string {
  return String(name || '').split(/[?#]/, 1)[0] || '';
}

export function isMarkdownPreviewFile(name: string): boolean {
  return MARKDOWN_FILE_RE.test(cleanFileName(name));
}

export function shouldRenderMarkdownPreview(name: string, sourceMode: boolean): boolean {
  return isMarkdownPreviewFile(name) && !sourceMode;
}

function isSafeExternalHref(href: string): boolean {
  return /^(https?:|mailto:)/i.test(href);
}

function isSafeExternalImageSrc(href: string): boolean {
  return /^https?:/i.test(href);
}

function splitUrlPath(rawUrl: string): string {
  return String(rawUrl || '').trim().split(/[?#]/, 1)[0] || '';
}

function currentDirectory(currentPath: string): string[] {
  const clean = cleanFileName(currentPath).replace(/\\/g, '/');
  const parts = clean.split('/').filter(Boolean);
  if (parts.length) parts.pop();
  return parts;
}

function normalizeWorkspacePath(currentPath: string, rawUrl: string): string {
  const rawPath = splitUrlPath(rawUrl).replace(/\\/g, '/');
  if (!rawPath || rawPath.startsWith('//')) return '';

  const parts = rawPath.startsWith('/') ? [] : currentDirectory(currentPath);
  for (const part of rawPath.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (!parts.length) return '';
      parts.pop();
      continue;
    }
    if (part.includes('\0')) return '';
    parts.push(part);
  }
  return parts.join('/');
}

export function resolveMarkdownUrl(rawUrl: string, currentPath = ''): MarkdownUrlResolution {
  const href = String(rawUrl || '').trim();
  if (!href) return { kind: 'unsafe', href: '' };
  if (href.startsWith('#')) return { kind: 'anchor', href };
  if (isSafeExternalHref(href)) return { kind: 'external', href };
  if (/^[a-z][a-z0-9+.-]*:/i.test(href)) return { kind: 'unsafe', href: '' };

  const path = normalizeWorkspacePath(currentPath, href);
  if (!path) return { kind: 'unsafe', href: '' };
  return { kind: 'workspace', href: path, path };
}

function buildTree(source: string): ParsedMarkdownNode {
  const tree = markdownGfmParser.parse(source);
  const cursor = tree.cursor();

  function read(): ParsedMarkdownNode {
    const node: ParsedMarkdownNode = {
      name: cursor.name,
      from: cursor.from,
      to: cursor.to,
      children: [],
    };
    if (cursor.firstChild()) {
      do {
        node.children.push(read());
      } while (cursor.nextSibling());
      cursor.parent();
    }
    return node;
  }

  return read();
}

function nodeKey(node: ParsedMarkdownNode, index = 0): string {
  return `${node.name}:${node.from}:${node.to}:${index}`;
}

function decodeHtmlEntities(value: string): string {
  return String(value || '').replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos);/gi, (match, entity) => {
    const normalized = String(entity || '').toLowerCase();
    if (normalized === 'amp') return '&';
    if (normalized === 'lt') return '<';
    if (normalized === 'gt') return '>';
    if (normalized === 'quot') return '"';
    if (normalized === 'apos') return "'";
    if (normalized.startsWith('#x')) {
      const code = Number.parseInt(normalized.slice(2), 16);
      return Number.isFinite(code) ? String.fromCodePoint(code) : match;
    }
    if (normalized.startsWith('#')) {
      const code = Number.parseInt(normalized.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : match;
    }
    return match;
  });
}

function trimTextEdges(nodes: ReactNode[]): ReactNode[] {
  const next = nodes.slice();
  while (next.length && typeof next[0] === 'string' && !String(next[0]).trim()) {
    next.shift();
  }
  while (next.length && typeof next[next.length - 1] === 'string' && !String(next[next.length - 1]).trim()) {
    next.pop();
  }
  if (typeof next[0] === 'string') next[0] = String(next[0]).replace(/^\s+/, '');
  const last = next.length - 1;
  if (last >= 0 && typeof next[last] === 'string') next[last] = String(next[last]).replace(/\s+$/, '');
  return next;
}

function childrenWithin(node: ParsedMarkdownNode, from: number, to: number): ParsedMarkdownNode[] {
  return node.children.filter((child) => child.from >= from && child.to <= to);
}

function findUrlNode(node: ParsedMarkdownNode): ParsedMarkdownNode | undefined {
  return node.children.find((child) => child.name === 'URL');
}

const RAW_HTML_ALLOWED_TAGS = new Set([
  'a',
  'b',
  'blockquote',
  'br',
  'code',
  'del',
  'div',
  'em',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'hr',
  'i',
  'img',
  'li',
  'ol',
  'p',
  'pre',
  'span',
  'strong',
  'sub',
  'sup',
  'table',
  'tbody',
  'td',
  'th',
  'thead',
  'tr',
  'u',
  'ul',
]);

const RAW_HTML_VOID_TAGS = new Set(['br', 'hr', 'img']);
const RAW_HTML_DROP_CONTENT_TAGS = new Set(['iframe', 'object', 'script', 'style']);
const RAW_HTML_GLOBAL_ATTRS = new Set(['align', 'aria-label', 'colspan', 'height', 'rowspan', 'title', 'width']);

type RawHtmlTagToken = {
  tag: string;
  closing: boolean;
  selfClosing: boolean;
};

function rawHtmlTagToken(source: string, node: ParsedMarkdownNode): RawHtmlTagToken | null {
  const raw = source.slice(node.from, node.to).trim();
  const match = raw.match(/^<\s*(\/)?\s*([A-Za-z][A-Za-z0-9:-]*)\b[\s\S]*?>$/);
  if (!match) return null;
  const tag = String(match[2] || '').toLowerCase();
  if (!tag) return null;
  return {
    tag,
    closing: Boolean(match[1]),
    selfClosing: /\/\s*>$/.test(raw),
  };
}

function matchingRawHtmlCloseIndex(source: string, nodes: ParsedMarkdownNode[], startIndex: number): number {
  const opening = rawHtmlTagToken(source, nodes[startIndex]);
  if (!opening || opening.closing || opening.selfClosing || RAW_HTML_VOID_TAGS.has(opening.tag)) return -1;

  let depth = 1;
  for (let index = startIndex + 1; index < nodes.length; index += 1) {
    const node = nodes[index];
    if (node.name !== 'HTMLTag') continue;
    const token = rawHtmlTagToken(source, node);
    if (!token || token.tag !== opening.tag) continue;
    if (token.closing) {
      depth -= 1;
      if (depth === 0) return index;
    } else if (!token.selfClosing && !RAW_HTML_VOID_TAGS.has(token.tag)) {
      depth += 1;
    }
  }
  return -1;
}

function buildHtmlTree(source: string): ParsedHtmlNode {
  const tree = htmlParser.parse(source);
  const cursor = tree.cursor();

  function read(): ParsedHtmlNode {
    const node: ParsedHtmlNode = {
      name: cursor.name,
      from: cursor.from,
      to: cursor.to,
      children: [],
    };
    if (cursor.firstChild()) {
      do {
        node.children.push(read());
      } while (cursor.nextSibling());
      cursor.parent();
    }
    return node;
  }

  return read();
}

function htmlTagName(source: string, node: ParsedHtmlNode): string {
  const tag = node.children.find((child) => child.name === 'TagName');
  return tag ? source.slice(tag.from, tag.to).toLowerCase() : '';
}

function htmlAttributeValue(source: string, node: ParsedHtmlNode): string {
  const value = node.children.find((child) => child.name === 'AttributeValue');
  if (!value) return '';
  const raw = source.slice(value.from, value.to);
  const unquoted = (raw.startsWith('"') && raw.endsWith('"')) || (raw.startsWith("'") && raw.endsWith("'"))
    ? raw.slice(1, -1)
    : raw;
  return decodeHtmlEntities(unquoted);
}

function htmlAttributes(source: string, node: ParsedHtmlNode): Record<string, string> {
  const attrs: Record<string, string> = {};
  node.children
    .filter((child) => child.name === 'Attribute')
    .forEach((attr) => {
      const nameNode = attr.children.find((child) => child.name === 'AttributeName');
      if (!nameNode) return;
      const name = source.slice(nameNode.from, nameNode.to).toLowerCase();
      if (!name || name.startsWith('on') || name === 'style') return;
      attrs[name] = htmlAttributeValue(source, attr);
    });
  return attrs;
}

function sanitizeRawHtmlAttrs(
  tag: string,
  attrs: Record<string, string>,
  currentPath: string,
  options: MarkdownPreviewProps,
): Record<string, unknown> {
  const props: Record<string, unknown> = {};

  for (const [name, value] of Object.entries(attrs)) {
    if (name === 'href' && tag === 'a') {
      const resolution = resolveMarkdownUrl(value, currentPath);
      if (resolution.kind === 'external') {
        props.href = resolution.href;
        props.target = '_blank';
        props.rel = 'noreferrer';
      } else if (resolution.kind === 'anchor') {
        props.href = resolution.href;
      } else if (resolution.kind === 'workspace') {
        props.href = '#';
        props['data-workspace-path'] = resolution.path;
        props.onClick = (event: React.MouseEvent<HTMLAnchorElement>) => {
          if (!options.onOpenWorkspacePath) return;
          event.preventDefault();
          options.onOpenWorkspacePath(resolution.path);
        };
      }
      continue;
    }

    if (name === 'src' && tag === 'img') {
      const resolution = resolveMarkdownUrl(value, currentPath);
      if (resolution.kind === 'external' && isSafeExternalImageSrc(resolution.href)) {
        props.src = resolution.href;
      } else if (resolution.kind === 'workspace' && options.imageSrcForPath) {
        props.src = options.imageSrcForPath(resolution.path);
      }
      continue;
    }

    if (tag === 'img' && (name === 'alt' || name === 'title' || name === 'width' || name === 'height')) {
      props[name] = value;
      continue;
    }

    if (RAW_HTML_GLOBAL_ATTRS.has(name)) {
      props[name] = value;
    }
  }

  if (tag === 'img' && !props.src) return {};
  return props;
}

function renderRawHtmlTag(
  source: string,
  currentPath: string,
  node: ParsedHtmlNode,
  options: MarkdownPreviewProps,
  index: number,
  children?: ReactNode[],
): ReactNode {
  const tag = htmlTagName(source, node);
  if (!tag) return null;
  if (RAW_HTML_DROP_CONTENT_TAGS.has(tag)) return source.slice(node.from, node.to);
  if (!RAW_HTML_ALLOWED_TAGS.has(tag)) return children || null;

  const props = sanitizeRawHtmlAttrs(tag, htmlAttributes(source, node), currentPath, options);
  if (tag === 'img' && !props.src) return props.alt ? String(props.alt) : null;
  props.key = nodeKey(node, index);

  if (RAW_HTML_VOID_TAGS.has(tag)) return React.createElement(tag, props);
  return React.createElement(tag, props, children);
}

function renderRawHtmlNode(
  source: string,
  currentPath: string,
  node: ParsedHtmlNode,
  options: MarkdownPreviewProps,
  index = 0,
): ReactNode {
  if (node.name === 'Document') {
    return node.children.map((child, childIndex) => renderRawHtmlNode(source, currentPath, child, options, childIndex));
  }
  if (node.name === 'Text') return decodeHtmlEntities(source.slice(node.from, node.to));
  if (node.name === 'Comment' || node.name === 'MismatchedCloseTag') return null;
  if (node.name === 'SelfClosingTag') return renderRawHtmlTag(source, currentPath, node, options, index);
  if (node.name === 'OpenTag') return renderRawHtmlTag(source, currentPath, node, options, index);

  if (node.name === 'Element') {
    const tagNode = node.children.find((child) => child.name === 'OpenTag' || child.name === 'SelfClosingTag');
    if (!tagNode) return null;
    const tag = htmlTagName(source, tagNode);
    if (RAW_HTML_DROP_CONTENT_TAGS.has(tag)) return source.slice(node.from, node.to);
    const children = node.children
      .filter((child) => child !== tagNode && child.name !== 'CloseTag')
      .map((child, childIndex) => renderRawHtmlNode(source, currentPath, child, options, childIndex));
    return renderRawHtmlTag(source, currentPath, tagNode, options, index, children);
  }

  return null;
}

function renderRawHtmlFragment(
  rawHtml: string,
  currentPath: string,
  options: MarkdownPreviewProps,
  index = 0,
): ReactNode {
  const root = buildHtmlTree(rawHtml);
  return renderRawHtmlNode(rawHtml, currentPath, root, options, index);
}

function renderInlineRange(
  source: string,
  currentPath: string,
  node: ParsedMarkdownNode,
  from: number,
  to: number,
  options: MarkdownPreviewProps,
): ReactNode[] {
  const nodes = childrenWithin(node, from, to);
  const rendered: ReactNode[] = [];
  let offset = from;

  for (let index = 0; index < nodes.length; index += 1) {
    const child = nodes[index];
    if (child.from > offset) rendered.push(source.slice(offset, child.from));

    if (child.name === 'HTMLTag') {
      const closeIndex = matchingRawHtmlCloseIndex(source, nodes, index);
      if (closeIndex > index) {
        const close = nodes[closeIndex];
        const childNode = renderRawHtmlFragment(source.slice(child.from, close.to), currentPath, options, index);
        if (Array.isArray(childNode)) rendered.push(...childNode);
        else if (childNode !== null) rendered.push(childNode);
        offset = close.to;
        index = closeIndex;
        continue;
      }
    }

    const childNode = renderInlineNode(source, currentPath, child, options, index);
    if (Array.isArray(childNode)) rendered.push(...childNode);
    else if (childNode !== null) rendered.push(childNode);
    offset = child.to;
  }

  if (offset < to) rendered.push(source.slice(offset, to));
  return rendered;
}

function renderTextWithoutMarks(
  source: string,
  currentPath: string,
  node: ParsedMarkdownNode,
  options: MarkdownPreviewProps,
): ReactNode[] {
  return trimTextEdges(renderInlineRange(source, currentPath, node, node.from, node.to, options));
}

function renderInlineNode(
  source: string,
  currentPath: string,
  node: ParsedMarkdownNode,
  options: MarkdownPreviewProps,
  index = 0,
): ReactNode | ReactNode[] | null {
  if (node.name.endsWith('Mark') || node.name === 'HeaderMark') return null;

  if (node.name === 'InlineCode') {
    const marks = node.children.filter((child) => child.name === 'CodeMark');
    const from = marks[0] ? marks[0].to : node.from;
    const to = marks[1] ? marks[1].from : node.to;
    return React.createElement('code', { key: nodeKey(node, index) }, source.slice(from, to));
  }

  if (node.name === 'Emphasis' || node.name === 'StrongEmphasis' || node.name === 'Strikethrough') {
    const tag = node.name === 'StrongEmphasis' ? 'strong' : node.name === 'Strikethrough' ? 'del' : 'em';
    return React.createElement(
      tag,
      { key: nodeKey(node, index) },
      renderTextWithoutMarks(source, currentPath, node, options),
    );
  }

  if (node.name === 'Link') {
    const urlNode = findUrlNode(node);
    if (!urlNode) return source.slice(node.from, node.to);
    const labelTo = source.lastIndexOf(']', urlNode.from);
    const labelFrom = node.from + 1;
    const label = renderInlineRange(source, currentPath, node, labelFrom, labelTo > labelFrom ? labelTo : urlNode.from, options);
    const resolution = resolveMarkdownUrl(source.slice(urlNode.from, urlNode.to), currentPath);

    if (resolution.kind === 'external') {
      return React.createElement(
        'a',
        { key: nodeKey(node, index), href: resolution.href, target: '_blank', rel: 'noreferrer' },
        label,
      );
    }
    if (resolution.kind === 'anchor') {
      return React.createElement('a', { key: nodeKey(node, index), href: resolution.href }, label);
    }
    if (resolution.kind === 'workspace') {
      return React.createElement(
        'a',
        {
          key: nodeKey(node, index),
          href: '#',
          'data-workspace-path': resolution.path,
          onClick: (event: React.MouseEvent<HTMLAnchorElement>) => {
            if (!options.onOpenWorkspacePath) return;
            event.preventDefault();
            options.onOpenWorkspacePath(resolution.path);
          },
        },
        label,
      );
    }
    return React.createElement('span', { key: nodeKey(node, index) }, label);
  }

  if (node.name === 'Image') {
    const urlNode = findUrlNode(node);
    if (!urlNode) return null;
    const labelTo = source.lastIndexOf(']', urlNode.from);
    const alt = source.slice(node.from + 2, labelTo > node.from ? labelTo : urlNode.from);
    const resolution = resolveMarkdownUrl(source.slice(urlNode.from, urlNode.to), currentPath);
    let src = '';
    if (resolution.kind === 'external' && isSafeExternalImageSrc(resolution.href)) src = resolution.href;
    if (resolution.kind === 'workspace' && options.imageSrcForPath) src = options.imageSrcForPath(resolution.path);
    if (!src) return React.createElement('span', { key: nodeKey(node, index) }, alt);
    return React.createElement('img', { key: nodeKey(node, index), src, alt });
  }

  if (node.name === 'URL') {
    const raw = source.slice(node.from, node.to);
    const href = /^www\./i.test(raw) ? `https://${raw}` : raw;
    const resolution = resolveMarkdownUrl(href, currentPath);
    if (resolution.kind !== 'external') return raw;
    return React.createElement(
      'a',
      { key: nodeKey(node, index), href: resolution.href, target: '_blank', rel: 'noreferrer' },
      raw,
    );
  }

  if (node.name === 'HTMLTag') {
    return renderRawHtmlFragment(source.slice(node.from, node.to), currentPath, options, index);
  }

  if (node.children.length) return renderTextWithoutMarks(source, currentPath, node, options);
  return source.slice(node.from, node.to);
}

function renderTableCell(source: string, currentPath: string, cell: ParsedMarkdownNode, options: MarkdownPreviewProps, tag: 'th' | 'td', index: number): ReactElement {
  return React.createElement(
    tag,
    { key: nodeKey(cell, index) },
    trimTextEdges(renderInlineRange(source, currentPath, cell, cell.from, cell.to, options)),
  );
}

function renderTable(source: string, currentPath: string, node: ParsedMarkdownNode, options: MarkdownPreviewProps): ReactElement {
  const header = node.children.find((child) => child.name === 'TableHeader');
  const rows = node.children.filter((child) => child.name === 'TableRow');

  return React.createElement(
    'table',
    { key: nodeKey(node) },
    header
      ? React.createElement(
          'thead',
          { key: 'thead' },
          React.createElement(
            'tr',
            null,
            header.children
              .filter((child) => child.name === 'TableCell')
              .map((cell, index) => renderTableCell(source, currentPath, cell, options, 'th', index)),
          ),
        )
      : null,
    rows.length
      ? React.createElement(
          'tbody',
          { key: 'tbody' },
          rows.map((row, rowIndex) =>
            React.createElement(
              'tr',
              { key: nodeKey(row, rowIndex) },
              row.children
                .filter((child) => child.name === 'TableCell')
                .map((cell, index) => renderTableCell(source, currentPath, cell, options, 'td', index)),
            ),
          ),
        )
      : null,
  );
}

function renderBlock(source: string, currentPath: string, node: ParsedMarkdownNode, options: MarkdownPreviewProps, index = 0): ReactNode {
  if (/^ATXHeading[1-6]$/.test(node.name)) {
    const level = Number(node.name.slice(-1));
    return React.createElement(
      `h${level}`,
      { key: nodeKey(node, index) },
      renderTextWithoutMarks(source, currentPath, node, options),
    );
  }

  if (node.name === 'Paragraph') {
    return React.createElement('p', { key: nodeKey(node, index) }, renderTextWithoutMarks(source, currentPath, node, options));
  }

  if (node.name === 'HTMLBlock') {
    return React.createElement(React.Fragment, { key: nodeKey(node, index) }, renderRawHtmlFragment(source.slice(node.from, node.to), currentPath, options, index));
  }

  if (node.name === 'CommentBlock') return null;

  if (node.name === 'FencedCode' || node.name === 'CodeBlock') {
    const code = node.children
      .filter((child) => child.name === 'CodeText')
      .map((child) => source.slice(child.from, child.to))
      .join('\n');
    return React.createElement('pre', { key: nodeKey(node, index) }, React.createElement('code', null, code));
  }

  if (node.name === 'BulletList' || node.name === 'OrderedList') {
    const tag = node.name === 'BulletList' ? 'ul' : 'ol';
    return React.createElement(
      tag,
      { key: nodeKey(node, index) },
      node.children
        .filter((child) => child.name === 'ListItem')
        .map((child, childIndex) => renderBlock(source, currentPath, child, options, childIndex)),
    );
  }

  if (node.name === 'ListItem') {
    const content = node.children.filter((child) => child.name !== 'ListMark');
    return React.createElement(
      'li',
      { key: nodeKey(node, index) },
      content.map((child, childIndex) => renderBlock(source, currentPath, child, options, childIndex)),
    );
  }

  if (node.name === 'Task') {
    const marker = node.children.find((child) => child.name === 'TaskMarker');
    const checked = marker ? /\[[xX]\]/.test(source.slice(marker.from, marker.to)) : false;
    const contentFrom = marker ? marker.to : node.from;
    return React.createElement(
      React.Fragment,
      { key: nodeKey(node, index) },
      React.createElement('input', { type: 'checkbox', checked, readOnly: true }),
      ' ',
      trimTextEdges(renderInlineRange(source, currentPath, node, contentFrom, node.to, options)),
    );
  }

  if (node.name === 'Blockquote') {
    return React.createElement(
      'blockquote',
      { key: nodeKey(node, index) },
      node.children
        .filter((child) => child.name !== 'QuoteMark')
        .map((child, childIndex) => renderBlock(source, currentPath, child, options, childIndex)),
    );
  }

  if (node.name === 'HorizontalRule') return React.createElement('hr', { key: nodeKey(node, index) });
  if (node.name === 'Table') return renderTable(source, currentPath, node, options);

  if (node.children.length) {
    return node.children.map((child, childIndex) => renderBlock(source, currentPath, child, options, childIndex));
  }
  return React.createElement('p', { key: nodeKey(node, index) }, source.slice(node.from, node.to));
}

export function MarkdownPreview(props: MarkdownPreviewProps): ReactElement {
  const source = String(props.content || '').replace(/\r\n?/g, '\n');
  const root = buildTree(source);
  const style: CSSProperties = { fontSize: `${props.fontSize}px` };

  return React.createElement(
    'div',
    { className: 'markdown-preview', style },
    root.children.map((node, index) => renderBlock(source, props.path || '', node, props, index)),
  );
}

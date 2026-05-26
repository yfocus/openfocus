/* SPDX-License-Identifier: Apache-2.0 */

export const MAX_PREVIEW_NAVIGATION_SYMBOL_LENGTH = 200;

export type PreviewTextSelection = {
  from: number;
  to: number;
  head?: number;
  selectedText?: string;
};

export type PreviewSymbolContext = {
  relPath: string;
  path: string;
  line: number;
  column: number;
  selectedText: string;
  wordAtCursor: string;
  symbol: string;
};

type PreviewSymbolContextInput = {
  path: string;
  content: string;
  selection?: PreviewTextSelection;
};

type WordAtCursor = {
  text: string;
  from: number;
  to: number;
};

export function createEmptyPreviewSymbolContext(path = ''): PreviewSymbolContext {
  return {
    relPath: path,
    path,
    line: 1,
    column: 1,
    selectedText: '',
    wordAtCursor: '',
    symbol: '',
  };
}

export function createPreviewSymbolContext(input: PreviewSymbolContextInput): PreviewSymbolContext {
  const path = String(input.path || '');
  const content = String(input.content || '');
  const selection = normalizeSelection(input.selection, content.length);
  const selectedText = selection
    ? input.selection?.selectedText ?? content.slice(selection.from, selection.to)
    : '';
  const cursorOffset = selection?.head ?? selection?.from ?? 0;
  const word = wordAtCursor(content, cursorOffset);
  const selectedSymbol = navigationSymbolFromText(selectedText);
  const suppressFallback = Boolean(selectedText) && !/[A-Za-z0-9_$]/.test(selectedText);
  const fallbackSymbol = suppressFallback ? '' : navigationSymbolFromText(word.text);
  const symbol = selectedSymbol || fallbackSymbol;
  const symbolOffset = selectedSymbol ? selection?.from ?? cursorOffset : word.from;
  const position = offsetToLineColumn(content, symbol ? symbolOffset : cursorOffset);

  return {
    relPath: path,
    path,
    line: position.line,
    column: position.column,
    selectedText,
    wordAtCursor: word.text,
    symbol,
  };
}

export function navigationSymbolFromText(value: string): string {
  const symbol = String(value || '').trim();
  if (!symbol || symbol.length > MAX_PREVIEW_NAVIGATION_SYMBOL_LENGTH) return '';
  if (!/^[A-Za-z_$][A-Za-z0-9_$]*(?:(?:\.|::)[A-Za-z_$][A-Za-z0-9_$]*)*$/.test(symbol)) return '';
  return symbol;
}

export function wordAtCursor(content: string, offset: number): WordAtCursor {
  const text = String(content || '');
  const safeOffset = clampOffset(offset, text.length);
  let anchor = safeOffset;
  const current = text.charAt(anchor);
  if (!isIdentifierChar(current) && anchor > 0 && isCursorAfterIdentifier(current) && isIdentifierChar(text.charAt(anchor - 1))) {
    anchor -= 1;
  }
  if (!isIdentifierChar(text.charAt(anchor))) {
    return { text: '', from: safeOffset, to: safeOffset };
  }

  let from = anchor;
  let to = anchor + 1;
  while (from > 0 && isIdentifierChar(text.charAt(from - 1))) from -= 1;
  while (to < text.length && isIdentifierChar(text.charAt(to))) to += 1;
  return { text: text.slice(from, to), from, to };
}

function normalizeSelection(selection: PreviewTextSelection | undefined, contentLength: number): PreviewTextSelection | null {
  if (!selection) return null;
  const from = clampOffset(Math.min(selection.from, selection.to), contentLength);
  const to = clampOffset(Math.max(selection.from, selection.to), contentLength);
  const head = clampOffset(selection.head ?? selection.to, contentLength);
  return { ...selection, from, to, head };
}

function offsetToLineColumn(content: string, offset: number): { line: number; column: number } {
  const text = String(content || '');
  const safeOffset = clampOffset(offset, text.length);
  let line = 1;
  let lineStart = 0;
  for (let index = 0; index < safeOffset; index += 1) {
    if (text.charAt(index) === '\n') {
      line += 1;
      lineStart = index + 1;
    }
  }
  return { line, column: safeOffset - lineStart + 1 };
}

function clampOffset(value: number, max: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(Math.trunc(value), Math.max(0, max)));
}

function isIdentifierChar(value: string): boolean {
  return /^[A-Za-z0-9_$]$/.test(value);
}

function isCursorAfterIdentifier(value: string): boolean {
  return value === '' || /\s/.test(value);
}

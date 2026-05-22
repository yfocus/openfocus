# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import contextlib
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin

from ...db import session_scope
from ...models import RemoteTerminalOutput, RemoteTerminalSession
from ..agent_spaces import terminals as terminal_records

TERMINAL_HISTORY_PUBLIC_MAX_BYTES = 4 * 1024 * 1024


class TerminalGatewayError(Exception):
    """Base class for shared terminal gateway failures."""


class TerminalValidationError(TerminalGatewayError, ValueError):
    """Raised when route input is structurally invalid."""


class TerminalStartError(TerminalGatewayError):
    """Raised when Companion cannot start a terminal."""


class TerminalInputError(TerminalGatewayError):
    """Raised when Companion rejects terminal input."""


class TerminalMouseModeError(TerminalGatewayError):
    """Raised when Companion rejects mouse mode updates."""


class TerminalUnavailable(TerminalGatewayError, LookupError):
    """Raised when an existing terminal cannot satisfy the requested operation."""


class TtydProxyError(TerminalGatewayError):
    """Raised when ttyd proxy forwarding fails."""


@dataclass(frozen=True)
class TerminalStartResult:
    terminal_id: str
    name: str
    backend: str
    connect_url: str


@dataclass(frozen=True)
class TerminalInfo:
    terminal_id: str
    companion_id: int
    backend: str
    connect_url: str


@dataclass(frozen=True)
class TtydProxyTarget:
    proxy_prefix: str
    target_url: str


@dataclass(frozen=True)
class TtydHttpProxyResponse:
    body: bytes
    status_code: int
    headers: dict[str, str]
    media_type: str


def ttyd_embed_path(route_prefix: str, owner_id: int, terminal_id: str) -> str:
    prefix = "/" + str(route_prefix or "").strip("/")
    tid = quote(str(terminal_id or ""), safe="")
    return f"{prefix}/{int(owner_id)}/terminals/{tid}/ttyd/"


def ttyd_target_url(base_url: str, tail: str, query: str) -> str:
    base = str(base_url or "").rstrip("/") + "/"
    clean_tail = str(tail or "")
    if clean_tail:
        # ttyd is started with the same base path, so the proxy must forward it.
        base = urljoin(base, clean_tail.lstrip("/"))
    if query:
        base = base + "?" + str(query)
    return base


def ttyd_proxy_target(
    *,
    connect_url: str,
    route_prefix: str,
    owner_id: int,
    terminal_id: str,
    path: str = "",
    query: str = "",
) -> TtydProxyTarget:
    proxy_prefix = ttyd_embed_path(route_prefix, int(owner_id), terminal_id)
    target_tail = proxy_prefix.lstrip("/") + str(path or "")
    return TtydProxyTarget(
        proxy_prefix=proxy_prefix,
        target_url=ttyd_target_url(connect_url, target_tail, query),
    )


def ttyd_websocket_target_url(target_url: str) -> str:
    target = str(target_url or "")
    if target.startswith("http://"):
        return "ws://" + target[len("http://") :]
    if target.startswith("https://"):
        return "wss://" + target[len("https://") :]
    return target


def _filter_headers(headers: Mapping[str, str], excluded: set[str]) -> dict[str, str]:
    return {
        str(k): str(v)
        for k, v in dict(headers or {}).items()
        if str(k).lower() not in excluded
    }


def _header_get(headers: Mapping[str, str], name: str) -> str | None:
    wanted = str(name).lower()
    for key, value in dict(headers or {}).items():
        if str(key).lower() == wanted:
            return str(value)
    return None


async def proxy_ttyd_http_request(
    *,
    target_url: str,
    method: str,
    headers: Mapping[str, str],
    body: bytes,
    opener: Callable | None = None,
    timeout_seconds: float = 30.0,
) -> TtydHttpProxyResponse:
    request_headers = _filter_headers(
        headers,
        {"host", "connection", "content-length", "accept-encoding"},
    )
    open_url = opener or urllib.request.urlopen
    try:
        req = urllib.request.Request(
            str(target_url or ""),
            data=body if body else None,
            headers=request_headers,
            method=str(method or "GET"),
        )
        resp = await asyncio.to_thread(open_url, req, timeout=timeout_seconds)
        data = await asyncio.to_thread(resp.read)
        response_headers = _filter_headers(
            getattr(resp, "headers", {}) or {},
            {"content-encoding", "transfer-encoding", "connection", "content-length"},
        )
        media_type = (
            _header_get(getattr(resp, "headers", {}) or {}, "content-type")
            or "application/octet-stream"
        )
        data = maybe_inject_ttyd_bridge(data, media_type)
        return TtydHttpProxyResponse(
            body=data,
            status_code=int(getattr(resp, "status", 200) or 200),
            headers=response_headers,
            media_type=media_type,
        )
    except urllib.error.HTTPError as e:
        data = await asyncio.to_thread(e.read)
        media_type = (
            _header_get(e.headers or {}, "content-type") or "application/octet-stream"
        )
        return TtydHttpProxyResponse(
            body=data,
            status_code=int(e.code),
            headers=_filter_headers(
                e.headers or {},
                {
                    "content-encoding",
                    "transfer-encoding",
                    "connection",
                    "content-length",
                },
            ),
            media_type=media_type,
        )
    except Exception as e:
        raise TtydProxyError(f"ttyd proxy failed: {e}") from e


def terminal_payload(
    owner_id: int,
    terminal: RemoteTerminalSession,
    *,
    route_prefix: str,
) -> dict[str, Any]:
    out = terminal_records.terminal_payload(terminal)
    backend = str(getattr(terminal, "backend", "") or "ttyd").strip() or "ttyd"
    connect_url = str(getattr(terminal, "connect_url", "") or "").strip()
    terminal_id = str(terminal.terminal_id or "")
    if backend == "ttyd" and connect_url:
        out["embed_url"] = ttyd_embed_path(route_prefix, int(owner_id), terminal_id)
    return out


def ttyd_bridge_script() -> str:
    return r"""
<script>
(function(){
  if(window.__openfocusTtydBridgeInstalled) return;
  window.__openfocusTtydBridgeInstalled = true;

  function disableBeforeUnload(){
    try{ window.onbeforeunload = null; }catch(_){ }
  }

  try{
    const rawAdd = window.addEventListener.bind(window);
    window.addEventListener = function(type, listener, options){
      if(String(type || '').toLowerCase() === 'beforeunload') return;
      return rawAdd(type, listener, options);
    };
  }catch(_){ }

  function terminalIdFromLocation(){
    try{
      const m = String(window.location && window.location.pathname || '').match(/\/terminals\/([^/]+)\/ttyd(?:\/|$)/);
      return m ? decodeURIComponent(m[1]) : '';
    }catch(_){ return ''; }
  }

  function applyTerminalFontSize(raw){
    const n = Number(raw);
    if(!Number.isFinite(n)) return;
    const size = Math.max(10, Math.min(24, Math.round(n)));
    let style = document.getElementById('openfocus-terminal-font-style');
    if(!style){
      style = document.createElement('style');
      style.id = 'openfocus-terminal-font-style';
      document.head.appendChild(style);
    }
    style.textContent = '.xterm, .terminal, .xterm-rows, .xterm-screen { font-size: ' + size + 'px !important; }';
    try{ window.dispatchEvent(new Event('resize')); }catch(_){ }
  }

  window.addEventListener('message', function(event){
    try{
      if(event.origin !== window.location.origin) return;
      const data = event.data || {};
      if(data && data.type === 'openfocus:terminal-font-size') applyTerminalFontSize(data.fontSize);
    }catch(_){ }
  });

  function stripToken(raw){
    let s = String(raw || '').trim();
    while(s.length >= 2 && "'\"`".indexOf(s[0]) >= 0 && s[s.length - 1] === s[0]) s = s.slice(1, -1).trim();
    const pairs = { '(': ')', '[': ']', '{': '}', '<': '>' };
    let changed = true;
    while(changed && s.length >= 2){
      changed = false;
      const end = pairs[s[0]];
      if(end && s[s.length - 1] === end){
        s = s.slice(1, -1).trim();
        changed = true;
      }
    }
    while(/[.,;]$/.test(s)) s = s.slice(0, -1);
    if(/:\d+:$/.test(s)) s = s.slice(0, -1);
    return s;
  }

  function parseTarget(raw){
    let value = stripToken(raw);
    if(!value) return null;
    if(/^https?:\/\//i.test(value)) return { href: value, text: value };
    if(value[0] === '@' && value.length > 1) value = stripToken(value.slice(1));

    let cameFromFileUrl = false;
    if(/^file:\/\//i.test(value)){
      cameFromFileUrl = true;
      try{
        const url = new URL(value);
        value = decodeURIComponent(url.pathname || '');
      }catch(_){
        value = value.replace(/^file:\/\//i, '');
      }
    }

    let line = null;
    let column = null;
    const anchor = value.match(/#L(\d+)(?:C(\d+))?$/i);
    if(anchor){
      line = Number(anchor[1] || 0) || null;
      column = Number(anchor[2] || 0) || null;
      value = value.slice(0, anchor.index).trim();
    }else{
      const suffix = value.match(/:(\d+)(?::(\d+))?$/);
      if(suffix){
        line = Number(suffix[1] || 0) || null;
        column = Number(suffix[2] || 0) || null;
        value = value.slice(0, suffix.index).trim();
      }
    }

    value = stripToken(value);
    if(value[0] === '@' && value.length > 1) value = stripToken(value.slice(1));
    if(!value) return null;
    if(!cameFromFileUrl && !(/^\.{1,2}\//.test(value) || /^\//.test(value) || value.indexOf('/') >= 0 || /[A-Za-z0-9_.@+-]+\.[A-Za-z0-9_+-]{1,12}$/.test(value))) return null;
    return { path: value, text: raw, line: line, column: column };
  }

  function parseAnchorTarget(eventTarget){
    try{
      const el = eventTarget && eventTarget.closest ? eventTarget : (eventTarget && eventTarget.parentElement);
      const anchor = el && el.closest ? el.closest('a[href]') : null;
      if(!anchor) return null;
      const attr = anchor.getAttribute('href') || '';
      return parseTarget(attr) || parseTarget(anchor.href || '') || null;
    }catch(_){ return null; }
  }

  function isXtermLike(value){
    try{
      return !!(value && typeof value.registerLinkProvider === 'function' && value.buffer && value.buffer.active && typeof value.buffer.active.getLine === 'function');
    }catch(_){ return false; }
  }

  function discoverXterm(root, depth, seen){
    if(!root || depth < 0) return null;
    if(isXtermLike(root)) return root;
    if(typeof root !== 'object' && typeof root !== 'function') return null;
    if(seen.has(root)) return null;
    seen.add(root);
    const preferred = ['term', 'terminal', 'xterm', '_term', '_terminal', 'instance', 'client', 'app'];
    for(const key of preferred){
      try{
        const found = discoverXterm(root[key], depth - 1, seen);
        if(found) return found;
      }catch(_){ }
    }
    if(depth <= 0) return null;
    let keys = [];
    try{ keys = Object.keys(root).slice(0, 80); }catch(_){ keys = []; }
    for(const key of keys){
      if(preferred.indexOf(key) >= 0) continue;
      try{
        const found = discoverXterm(root[key], depth - 1, seen);
        if(found) return found;
      }catch(_){ }
    }
    return null;
  }

  function findXterm(){
    const seen = new WeakSet();
    const direct = discoverXterm(window, 3, seen);
    if(direct) return direct;
    try{
      const roots = document.querySelectorAll('.terminal, .xterm');
      for(const root of roots){
        const found = discoverXterm(root, 2, seen);
        if(found) return found;
      }
    }catch(_){ }
    return null;
  }

  function terminalLineElement(eventTarget){
    let el = eventTarget && eventTarget.nodeType === 1 ? eventTarget : (eventTarget && eventTarget.parentElement);
    while(el && el !== document.body){
      const parent = el.parentElement;
      if(parent && parent.classList && parent.classList.contains('xterm-rows')) return el;
      if(parent && parent.classList && parent.classList.contains('xterm-accessibility-tree')) return el;
      el = parent;
    }
    return null;
  }

  function rowFromContainerAtPoint(selector, x, y){
    try{
      const containers = Array.from(document.querySelectorAll(selector));
      for(const container of containers){
        const rect = container.getBoundingClientRect();
        if(x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) continue;
        const children = Array.from(container.children || []).filter((child)=> String(child.textContent || '').trim());
        if(!children.length) continue;
        let best = null;
        let bestDistance = Infinity;
        for(const child of children){
          const childRect = child.getBoundingClientRect();
          const inside = y >= childRect.top && y <= childRect.bottom;
          const distance = inside ? 0 : Math.min(Math.abs(y - childRect.top), Math.abs(y - childRect.bottom));
          if(distance < bestDistance){
            bestDistance = distance;
            best = child;
          }
        }
        if(best) return best;
      }
    }catch(_){ }
    return null;
  }

  function terminalLineElementAtPoint(event){
    return terminalLineElement(event.target)
      || rowFromContainerAtPoint('.xterm-rows', event.clientX, event.clientY)
      || rowFromContainerAtPoint('.xterm-accessibility-tree', event.clientX, event.clientY);
  }

  function estimateCellWidth(row){
    try{
      const probe = document.createElement('span');
      const style = window.getComputedStyle(row);
      probe.textContent = '0000000000';
      probe.style.position = 'absolute';
      probe.style.visibility = 'hidden';
      probe.style.whiteSpace = 'pre';
      probe.style.font = style.font;
      row.appendChild(probe);
      const width = probe.getBoundingClientRect().width / 10;
      probe.remove();
      if(width > 0) return width;
    }catch(_){ }
    return 8;
  }

  function candidateTokens(line){
    const text = String(line || '');
    if(!text) return [];
    const re = /(?:@?file:\/\/[^\s<>"'`\)\]\}]+|https?:\/\/[^\s<>"'`\)\]\}]+|@?(?:\.{1,2}\/|\/|[A-Za-z0-9_.@+-]+\/)[^\s<>"'`\)\]\}]+|@?[A-Za-z0-9_.@+-]+\.[A-Za-z0-9_+-]{1,16}(?::\d+){0,2}(?:#L\d+(?:C\d+)?)?)/g;
    const out = [];
    let match;
    while((match = re.exec(text))){
      const raw = match[0] || '';
      if(raw) out.push({ raw: raw, start: match.index || 0, end: (match.index || 0) + raw.length });
    }
    return out;
  }

  function linkCandidates(line){
    const out = [];
    for(const candidate of candidateTokens(line)){
      const target = parseTarget(candidate.raw);
      if(target) out.push({ raw: candidate.raw, start: candidate.start, end: candidate.end, target: target });
    }
    return out;
  }

  function tokenNearOffset(line, offset){
    const candidates = linkCandidates(line);
    if(!candidates.length) return '';
    let best = '';
    let bestDistance = Infinity;
    for(const candidate of candidates){
      const raw = candidate.raw || '';
      const start = candidate.start || 0;
      const end = candidate.end || start;
      if(offset >= start && offset <= end) return stripToken(raw);
      const distance = offset < start ? start - offset : offset - end;
      if(distance < bestDistance){
        bestDistance = distance;
        best = raw;
      }
    }
    return bestDistance <= 2 ? stripToken(best) : '';
  }

  function textOffsetFromNode(row, node, offset){
    try{
      const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
      let current;
      let total = 0;
      while((current = walker.nextNode())){
        if(current === node) return total + Math.max(0, Math.min(String(current.nodeValue || '').length, Number(offset || 0)));
        total += String(current.nodeValue || '').length;
      }
    }catch(_){ }
    return null;
  }

  function caretOffsetFromPoint(row, x, y){
    try{
      if(document.caretPositionFromPoint){
        const pos = document.caretPositionFromPoint(x, y);
        if(pos && pos.offsetNode && row.contains(pos.offsetNode)){
          const off = textOffsetFromNode(row, pos.offsetNode, pos.offset);
          if(Number.isFinite(off)) return off;
        }
      }
    }catch(_){ }
    try{
      if(document.caretRangeFromPoint){
        const range = document.caretRangeFromPoint(x, y);
        if(range && range.startContainer && row.contains(range.startContainer)){
          const off = textOffsetFromNode(row, range.startContainer, range.startOffset);
          if(Number.isFinite(off)) return off;
        }
      }
    }catch(_){ }
    return null;
  }

  function parseTextTarget(event){
    const row = terminalLineElementAtPoint(event);
    if(!row) return null;
    const text = String(row.textContent || '');
    if(!text) return null;
    const rect = row.getBoundingClientRect();
    const cellWidth = estimateCellWidth(row);
    const caretOffset = caretOffsetFromPoint(row, event.clientX, event.clientY);
    const offset = Number.isFinite(caretOffset) ? Number(caretOffset) : Math.max(0, Math.min(text.length, Math.floor((event.clientX - rect.left) / Math.max(1, cellWidth))));
    const token = tokenNearOffset(text, offset);
    return token ? parseTarget(token) : null;
  }

  let lastOpenKey = '';
  let lastOpenTs = 0;

  function postOpen(target){
    if(!target) return false;
    const key = [target.href || '', target.path || '', target.line || '', target.column || ''].join('|');
    const now = Date.now();
    if(key && key === lastOpenKey && now - lastOpenTs < 350) return true;
    lastOpenKey = key;
    lastOpenTs = now;
    const payload = {
      type: 'openfocus:terminal-link-open',
      href: target.href || '',
      text: target.text || '',
      path: target.path || '',
      line: target.line || null,
      column: target.column || null,
      terminalId: terminalIdFromLocation()
    };
    try{
      window.parent && window.parent.postMessage(payload, window.location.origin);
      return true;
    }catch(_){ return false; }
  }

  function consumeEvent(event){
    try{ event.preventDefault(); }catch(_){ }
    try{ event.stopPropagation(); }catch(_){ }
    try{ event.stopImmediatePropagation(); }catch(_){ }
  }

  function isCommandOpenEvent(event){
    if(!event || event.button !== 0) return;
    return !!(event.metaKey || event.ctrlKey);
  }

  function onCommandOpenEvent(event){
    if(!isCommandOpenEvent(event)) return;
    const target = parseAnchorTarget(event.target) || parseTextTarget(event);
    if(!target) return;
    if(!postOpen(target)) return;
    consumeEvent(event);
  }

  const registeredXterms = new WeakSet();

  function installXtermLinkProvider(){
    const term = findXterm();
    if(!term || registeredXterms.has(term)) return false;
    try{
      const disposable = term.registerLinkProvider({
        provideLinks: function(y, callback){
          try{
            const active = term.buffer && term.buffer.active;
            const line = active && active.getLine ? active.getLine(Number(active.baseY || 0) + Number(y || 1) - 1) : null;
            const text = line && line.translateToString ? line.translateToString(true) : '';
            const links = linkCandidates(text).map((candidate)=> ({
              text: candidate.raw,
              range: { start: { x: candidate.start + 1, y: Number(y || 1) }, end: { x: candidate.end, y: Number(y || 1) } },
              activate: function(event){
                if(!event || !(event.metaKey || event.ctrlKey)) return;
                if(postOpen(candidate.target)) consumeEvent(event);
              }
            }));
            callback(links);
          }catch(_){
            callback([]);
          }
        }
      });
      registeredXterms.add(term);
      try{ window.__openfocusTtydLinkProvider = disposable; }catch(_){ }
      return true;
    }catch(_){ return false; }
  }

  disableBeforeUnload();
  try{ setInterval(disableBeforeUnload, 1000); }catch(_){ }
  try{ document.addEventListener('pointerdown', onCommandOpenEvent, true); }catch(_){ }
  try{ document.addEventListener('mousedown', onCommandOpenEvent, true); }catch(_){ }
  try{ document.addEventListener('click', onCommandOpenEvent, true); }catch(_){ }
  try{
    let attempts = 0;
    const timer = setInterval(function(){
      attempts += 1;
      if(installXtermLinkProvider() || attempts > 40) clearInterval(timer);
    }, 250);
  }catch(_){ }
})();
</script>
"""


def maybe_inject_ttyd_bridge(data: bytes, media_type: str) -> bytes:
    mt = str(media_type or "").lower()
    if "text/html" not in mt:
        return data
    try:
        html = bytes(data or b"").decode("utf-8")
    except Exception:
        return data
    if "__openfocusTtydBridgeInstalled" in html:
        return data
    script = ttyd_bridge_script()
    lower = html.lower()
    i = lower.find("<head>")
    if i >= 0:
        j = i + len("<head>")
        html = html[:j] + script + html[j:]
    else:
        html = script + html
    return html.encode("utf-8")


def _clean_terminal_id(terminal_id: str) -> str:
    tid = str(terminal_id or "").strip()
    if not tid:
        raise TerminalValidationError("terminal_id is required")
    return tid


def _clean_terminal_name(name: str) -> str:
    raw_name = str(name or "").strip()
    if not raw_name:
        raise TerminalValidationError("name is required")
    if len(raw_name) > 128:
        raise TerminalValidationError("name is too long (<=128)")
    return raw_name


def _payload_bytes(payload: dict[str, Any] | None) -> bytes:
    body = payload or {}
    raw = b""
    data_b64 = str(body.get("data_b64") or "")
    if data_b64:
        with contextlib.suppress(Exception):
            raw = base64.b64decode(data_b64)
    if not raw:
        raw = str(body.get("text") or "").encode("utf-8")
    if not raw:
        raise TerminalValidationError("data is required")
    return raw


def _slice_from_last_sync_point(data: bytes) -> tuple[bytes, bool, str]:
    """Replay terminal history from a screen-safe point when possible."""

    b = bytes(data or b"")
    if not b:
        return b, False, ""

    alt_enter_markers = [
        b"\x1b[?1049h",
        b"\x1b[?1047h",
        b"\x1b[?47h",
    ]
    alt_exit_markers = [
        b"\x1b[?1049l",
        b"\x1b[?1047l",
        b"\x1b[?47l",
    ]

    last_alt_enter = max((b.rfind(pat) for pat in alt_enter_markers), default=-1)
    last_alt_exit = max((b.rfind(pat) for pat in alt_exit_markers), default=-1)
    if last_alt_enter > max(last_alt_exit, -1):
        return b[last_alt_enter:], True, "alt_screen_active"

    markers: list[tuple[bytes, str]] = [
        (b"\x1b[?1049h", "alt_screen"),
        (b"\x1b[?1047h", "alt_screen"),
        (b"\x1b[?47h", "alt_screen"),
        (b"\x1bc", "reset"),
        (b"\x1b[2J", "clear"),
    ]
    best = -1
    why = ""
    for pat, tag in markers:
        i = b.rfind(pat)
        if i > best:
            best = i
            why = tag
    if best <= 0:
        return b, False, ""
    return b[best:], True, why


def _terminal_ids_from_list_response(response: Any) -> set[str]:
    sessions = getattr(response, "sessions", []) or []
    return {
        str(getattr(session, "terminal_id", "") or "").strip()
        for session in sessions
        if str(getattr(session, "terminal_id", "") or "").strip()
    }


class RemoteTerminalGateway:
    def __init__(
        self,
        *,
        session_factory: Callable = session_scope,
        terminal_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._terminal_id_factory = terminal_id_factory or (lambda: str(uuid.uuid4()))

    def list_terminals(
        self, owner: terminal_records.TerminalOwner
    ) -> list[RemoteTerminalSession]:
        with self._session_factory() as s:
            return terminal_records.list_terminals(s, owner)

    async def list_live_terminals(
        self,
        *,
        owner: terminal_records.TerminalOwner,
        conn=None,
        conn_resolver: Callable[[int], Any] | None = None,
        timeout_seconds: float = 3.0,
        prune_stale: bool = True,
    ) -> list[RemoteTerminalSession]:
        with self._session_factory() as s:
            terminals = terminal_records.list_terminals(s, owner)

        if not terminals:
            return []

        live_ids: set[str] = set()
        checked_ids: set[str] = set()

        if conn_resolver is not None:
            by_companion: dict[int, list[str]] = {}
            for terminal in terminals:
                terminal_id = str(terminal.terminal_id or "").strip()
                companion_id = int(terminal.companion_id or 0)
                if terminal_id and companion_id:
                    by_companion.setdefault(companion_id, []).append(terminal_id)
            for companion_id, terminal_ids in by_companion.items():
                try:
                    resolved_conn = conn_resolver(int(companion_id))
                except Exception:
                    continue
                if resolved_conn is None:
                    continue
                try:
                    response = await resolved_conn.request_terminal_list_sessions(
                        timeout_seconds=timeout_seconds
                    )
                except Exception:
                    continue
                checked_ids.update(terminal_ids)
                live_ids.update(_terminal_ids_from_list_response(response))
        elif conn is not None:
            try:
                response = await conn.request_terminal_list_sessions(
                    timeout_seconds=timeout_seconds
                )
            except Exception:
                return []
            live_ids = _terminal_ids_from_list_response(response)
            checked_ids = {
                str(terminal.terminal_id or "").strip()
                for terminal in terminals
                if str(terminal.terminal_id or "").strip()
            }
        else:
            return []

        live_terminals = [
            terminal
            for terminal in terminals
            if str(terminal.terminal_id or "").strip() in live_ids
        ]

        if prune_stale:
            stale_ids = [
                str(terminal.terminal_id or "").strip()
                for terminal in terminals
                if str(terminal.terminal_id or "").strip()
                and str(terminal.terminal_id or "").strip() in checked_ids
                and str(terminal.terminal_id or "").strip() not in live_ids
            ]
            if stale_ids:
                with self._session_factory() as s:
                    for terminal_id in stale_ids:
                        with contextlib.suppress(terminal_records.TerminalNotFound):
                            terminal_records.delete_terminal_record(
                                s, owner=owner, terminal_id=terminal_id
                            )

        return live_terminals

    async def start_terminal(
        self,
        *,
        owner: terminal_records.TerminalOwner,
        conn,
        companion_id: int | None,
        root_path: str,
        base_path: str,
        task_public_id: str = "",
        timeout_seconds: float = 10.0,
    ) -> TerminalStartResult:
        terminal_id = str(self._terminal_id_factory())
        request_base_path = str(base_path or "").replace("{terminal_id}", terminal_id)
        try:
            res = await conn.request_terminal_start(
                terminal_id=terminal_id,
                root_path=str(root_path or ""),
                base_path=request_base_path,
                task_public_id=str(task_public_id or ""),
                timeout_seconds=timeout_seconds,
            )
        except Exception as e:
            raise TerminalStartError(f"Companion terminal failed to start: {e}") from e

        real_tid = str(getattr(res, "terminal_id", "") or "").strip() or terminal_id
        backend = str(getattr(res, "backend", "") or "ttyd").strip() or "ttyd"
        connect_url = str(getattr(res, "connect_url", "") or "").strip()
        if backend == "ttyd" and not connect_url:
            with contextlib.suppress(Exception):
                await conn.request_terminal_stop(
                    terminal_id=real_tid, timeout_seconds=timeout_seconds
                )
            raise TerminalStartError(
                "Companion terminal failed to start: missing connect_url"
            )
        try:
            with self._session_factory() as s:
                terminal = terminal_records.create_terminal_record(
                    s,
                    owner=owner,
                    task_public_id=str(task_public_id or ""),
                    companion_id=int(companion_id) if companion_id else None,
                    root_path=str(root_path or ""),
                    terminal_id=real_tid,
                    backend=backend,
                    connect_url=connect_url,
                )
                name = str(terminal.name or "")
        except Exception:
            with contextlib.suppress(Exception):
                await conn.request_terminal_stop(
                    terminal_id=real_tid, timeout_seconds=timeout_seconds
                )
            raise

        return TerminalStartResult(
            terminal_id=real_tid,
            name=name,
            backend=backend,
            connect_url=connect_url,
        )

    def rename_terminal(
        self,
        *,
        owner: terminal_records.TerminalOwner,
        terminal_id: str,
        name: str,
    ) -> str:
        tid = _clean_terminal_id(terminal_id)
        raw_name = _clean_terminal_name(name)
        with self._session_factory() as s:
            terminal_records.rename_terminal(
                s, owner=owner, terminal_id=tid, name=raw_name
            )
        return raw_name

    def terminal_info(
        self, *, owner: terminal_records.TerminalOwner, terminal_id: str
    ) -> TerminalInfo:
        tid = _clean_terminal_id(terminal_id)
        with self._session_factory() as s:
            terminal = terminal_records.get_terminal_for_owner(
                s, owner=owner, terminal_id=tid
            )
            return TerminalInfo(
                terminal_id=tid,
                companion_id=int(terminal.companion_id or 0),
                backend=str(getattr(terminal, "backend", "") or "ttyd").strip()
                or "ttyd",
                connect_url=str(getattr(terminal, "connect_url", "") or "").strip(),
            )

    def parse_input_payload(self, payload: dict[str, Any] | None) -> bytes:
        return _payload_bytes(payload)

    def load_history(
        self,
        *,
        owner: terminal_records.TerminalOwner,
        terminal_id: str,
        max_bytes: int = TERMINAL_HISTORY_PUBLIC_MAX_BYTES,
    ) -> dict[str, Any]:
        tid = _clean_terminal_id(terminal_id)
        max_bytes = max(
            1024, min(int(max_bytes or 0), TERMINAL_HISTORY_PUBLIC_MAX_BYTES)
        )
        with self._session_factory() as s:
            terminal_records.get_terminal_for_owner(s, owner=owner, terminal_id=tid)
            rows = (
                s.query(RemoteTerminalOutput)
                .filter(RemoteTerminalOutput.terminal_id == tid)
                .order_by(RemoteTerminalOutput.id.desc())
                .all()
            )

        buf: list[bytes] = []
        total = 0
        truncated = False
        for row in rows:
            try:
                b = base64.b64decode(str(row.data_b64 or ""))
            except Exception:
                b = b""
            if not b:
                continue
            if total + len(b) > max_bytes:
                truncated = True
                break
            buf.append(b)
            total += len(b)
        buf.reverse()
        raw = b"".join(buf) if buf else b""

        sliced, sliced_ok, sliced_reason = _slice_from_last_sync_point(raw)
        if sliced_ok:
            raw = sliced

        out_b64 = base64.b64encode(raw).decode("ascii") if raw else ""
        return {
            "ok": True,
            "terminal_id": tid,
            "data_b64": out_b64,
            "truncated": truncated,
            "sync_sliced": bool(sliced_ok),
            "sync_reason": str(sliced_reason or ""),
        }

    async def inject_input(
        self,
        *,
        owner: terminal_records.TerminalOwner,
        terminal_id: str,
        payload: dict[str, Any] | None,
        conn,
        timeout_seconds: float = 10.0,
    ) -> bytes:
        tid = _clean_terminal_id(terminal_id)
        self.terminal_info(owner=owner, terminal_id=tid)
        raw = _payload_bytes(payload)
        try:
            await conn.request_terminal_input(
                terminal_id=tid, data=raw, timeout_seconds=timeout_seconds
            )
        except Exception as e:
            raise TerminalInputError(f"terminal inject failed: {e}") from e
        return raw

    async def set_mouse_mode(
        self,
        *,
        owner: terminal_records.TerminalOwner,
        terminal_id: str,
        enabled: bool,
        conn,
        timeout_seconds: float = 10.0,
    ) -> bool:
        tid = _clean_terminal_id(terminal_id)
        self.terminal_info(owner=owner, terminal_id=tid)
        try:
            res = await conn.request_terminal_mouse_mode(
                terminal_id=tid,
                enabled=bool(enabled),
                timeout_seconds=timeout_seconds,
            )
        except Exception as e:
            raise TerminalMouseModeError(f"terminal mouse mode failed: {e}") from e
        return bool(getattr(res, "enabled", enabled))

    async def close_terminal(
        self,
        *,
        owner: terminal_records.TerminalOwner,
        terminal_id: str,
        conn=None,
        clear_auto_prompt: Callable[[str], None] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        tid = _clean_terminal_id(terminal_id)
        self.terminal_info(owner=owner, terminal_id=tid)
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.request_terminal_stop(
                    terminal_id=tid, timeout_seconds=timeout_seconds
                )
        with self._session_factory() as s:
            terminal_records.delete_terminal_record(s, owner=owner, terminal_id=tid)
        if clear_auto_prompt is not None:
            clear_auto_prompt(tid)

    async def release_owner_terminals(
        self,
        *,
        owner: terminal_records.TerminalOwner,
        conn=None,
        conn_resolver: Callable[[int], Any] | None = None,
        clear_auto_prompt: Callable[[str], None] | None = None,
        timeout_seconds: float = 5.0,
        delete_local_records: bool = True,
    ) -> list[str]:
        with self._session_factory() as s:
            terminals = terminal_records.list_terminals(s, owner)
            terminal_infos = [
                (str(t.terminal_id or "").strip(), int(t.companion_id or 0))
                for t in terminals
                if str(t.terminal_id or "").strip()
            ]
            terminal_ids = [tid for tid, _ in terminal_infos]

        if (conn is not None or conn_resolver is not None) and terminal_ids:

            async def _stop_one(tid: str, companion_id: int) -> None:
                stop_conn = conn
                if conn_resolver is not None:
                    if not companion_id:
                        return
                    try:
                        stop_conn = conn_resolver(int(companion_id or 0))
                    except Exception:
                        return
                if stop_conn is None:
                    return
                with contextlib.suppress(Exception):
                    await stop_conn.request_terminal_stop(
                        terminal_id=tid, timeout_seconds=timeout_seconds
                    )

            await asyncio.gather(
                *[_stop_one(tid, companion_id) for tid, companion_id in terminal_infos],
                return_exceptions=True,
            )

        if delete_local_records:
            with self._session_factory() as s:
                terminal_records.delete_owner_terminal_records(s, owner=owner)

        if clear_auto_prompt is not None:
            for tid in terminal_ids:
                clear_auto_prompt(tid)
        return terminal_ids

    def load_ttyd_connect_url(
        self, *, owner: terminal_records.TerminalOwner, terminal_id: str
    ) -> str:
        info = self.terminal_info(owner=owner, terminal_id=terminal_id)
        if info.backend != "ttyd" or not info.connect_url:
            raise TerminalUnavailable("ttyd terminal not found")
        return info.connect_url.rstrip("/")

<!-- SPDX-License-Identifier: Apache-2.0 -->
# Terminals Domain SPEC

## Responsibility

The Terminals domain owns shared remote terminal behavior that is used by both
AgentSpace and InspirationSpace:

- owner-scoped terminal validation
- terminal start, rename, input injection, mouse mode, close, and record cleanup
- live terminal reconciliation against Companion runtime sessions
- owner release cleanup for all terminals under one AgentSpace or InspirationSpace
- terminal history replay with bounded output and screen-safe sync slicing
- terminal payload shaping with ttyd embed URLs
- terminal runtime command dispatch through a small runtime port interface
- ttyd proxy URL construction, protocol-neutral HTTP proxy forwarding, WebSocket
  target shaping, and HTML bridge injection helpers
- ttyd bridge behavior that integrates terminal file links and browser-side
  AgentSpace terminal display settings

## Boundaries

- Domain code must not import FastAPI request/response/websocket types.
- Routes translate `TerminalGatewayError` subclasses into HTTP status codes.
- Companion runtime objects are passed into the gateway through
  `TerminalRuntimePort` adapters; the gateway does not know the global Companion
  registry.
- Existing `domains.agent_spaces.terminals` remains the persistence helper for
  owner records while this module becomes the shared cross-workspace interface.
- `RemoteTerminalSession` records are durable metadata. Companion runtime
  sessions are the source of truth for whether a terminal is currently live.

## Invariants

- All terminal lookup and mutation must be scoped by `TerminalOwner`.
- `RemoteTerminalSession.space_id` is legacy compatibility data only; owner
  checks use `owner_type` and `owner_id`.
- Listing terminals for UI must ask Companion for live sessions and return only
  terminals confirmed by that runtime source. Stale local records may be
  removed after a successful runtime reconciliation.
- Input injection and mouse mode updates must resolve the runtime from terminal
  metadata inside the gateway when a caller supplies a runtime resolver.
- If a runtime command reports that a terminal no longer exists, the gateway must
  delete the local terminal metadata/output rows and raise `TerminalUnavailable`.
- Routes should map stale runtime `TerminalUnavailable` failures to HTTP 410 so
  clients stop retrying against expired local metadata.
- ttyd starts must not create local terminal records unless Companion returns a
  non-empty `connect_url`.
- Closing a terminal deletes its output rows and session row locally even when
  Companion stop is best-effort.
- Releasing an owner stops every terminal best-effort, deletes terminal output
  rows and session rows locally, and clears per-terminal auto prompt state when a
  caller provides that adapter.
- Terminal history must first validate owner membership, then replay at most the
  public byte limit and prefer a screen-safe sync point for TUI output.
- Input injection accepts either `data_b64` or UTF-8 `text`; empty input is
  rejected before calling Companion.
- ttyd proxying requires a non-empty `connect_url`; tests and non-iframe backends
  may create terminal records without one.
- ttyd HTTP/WebSocket proxying must also reconcile with Companion runtime before
  forwarding. A stale DB `connect_url` is not enough: when the Companion is
  offline, the runtime resolver is unavailable, or `TerminalListSessions` no
  longer contains the terminal id, the gateway must delete local terminal
  metadata/output rows and raise `TerminalUnavailable` so routes can return a
  stable stale/unavailable response instead of repeatedly proxying to dead ttyd
  ports.
- Stopping or disconnecting Companion must not cause Core log storms. Terminal
  routes should convert stale runtime state to bounded 410/404 responses rather
  than issuing tight proxy retries or repeatedly logging WARN/ERROR failures.
- Browser-side terminal font size updates must be applied through the live
  xterm instance options, then trigger terminal resize/refresh. The bridge must
  not resize terminal glyphs by injecting CSS against `.xterm` rows/screens,
  because that desynchronizes xterm cell measurement from rendered glyph width
  and can clip mixed-width output at the right edge.

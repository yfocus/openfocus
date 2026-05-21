<!-- SPDX-License-Identifier: Apache-2.0 -->
# Terminals Domain SPEC

## Responsibility

The Terminals domain owns shared remote terminal behavior that is used by both
AgentSpace and InspirationSpace:

- owner-scoped terminal validation
- terminal start, rename, input injection, mouse mode, close, and record cleanup
- terminal payload shaping with ttyd embed URLs
- ttyd proxy URL construction and HTML bridge injection helpers

## Boundaries

- Domain code must not import FastAPI request/response/websocket types.
- Routes translate `TerminalGatewayError` subclasses into HTTP status codes.
- Companion connections are passed into the gateway as adapters; the gateway does
  not know the global Companion registry.
- Existing `domains.agent_spaces.terminals` remains the persistence helper for
  owner records while this module becomes the shared cross-workspace interface.

## Invariants

- All terminal lookup and mutation must be scoped by `TerminalOwner`.
- `RemoteTerminalSession.space_id` is legacy compatibility data only; owner
  checks use `owner_type` and `owner_id`.
- Closing a terminal deletes its output rows and session row locally even when
  Companion stop is best-effort.
- Input injection accepts either `data_b64` or UTF-8 `text`; empty input is
  rejected before calling Companion.
- ttyd proxying requires a non-empty `connect_url`; tests and non-iframe backends
  may create terminal records without one.

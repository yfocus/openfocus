<!-- SPDX-License-Identifier: Apache-2.0 -->
# Companion Module

`openfocus.companion` owns the local Companion runtime process and the trusted
bridge between OpenFocus Core and machine-local capabilities.

## Responsibilities

- Maintain the Companion gRPC client runtime, pairing state, reconnect loop, and
  runtime signal forwarding.
- Close server-side gRPC connection state promptly when either side disconnects
  or crashes; reconnection must rely on bounded backoff rather than tight retry
  or ping loops.
- Expose only declared local capabilities to OpenFocus Core, including terminal,
  agent workspace, directory selection, runtime hooks, and system float ball.
- Run the system `Inbox` float ball helper as a local UI process when the browser
  session has been bound to a trusted local Companion.

## System Float Ball

- The helper must use the OpenFocus web API as the activity source of truth:
  `/api/agent_activity/summary?limit=30`.
- The visible badge mirrors the web floating inbox counts: `R` is running agent
  activity and `W` is waiting/review activity.
- Clicking the ball opens an inbox panel with running spaces, waiting/review
  items, and NextMove recommendations. Item actions use the server-provided
  `action.primary_url` or fallback URL; dismiss uses `dismiss_url`.
- The inbox panel provides `Dashboard` and `Quit` controls. `Quit` stops the
  system float ball through OpenFocus and exits the local helper.
- The visible ball stays compact so it does not dominate the desktop; it shows
  only the `Inbox` label and running/waiting counts.
- The helper may keep an initial summary snapshot from gRPC start, but it should
  refresh from the web API so it does not diverge from the page-level inbox.
- The Companion records and reclaims helper processes by browser session id.
  Restarting or stopping the same system inbox session must terminate any stale
  helper left behind by a crashed or forcibly closed Companion.

## Non-Goals

- The Companion must not expose a browser-facing HTTP server.
- The float ball helper must not infer activity state from journal events; it
  only displays the `agent_activity` read model returned by OpenFocus Core.

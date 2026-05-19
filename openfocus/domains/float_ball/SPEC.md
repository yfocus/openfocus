<!-- SPDX-License-Identifier: Apache-2.0 -->
# Float Ball Domain

Owns System Inbox target selection and system float ball orchestration.

- The System Inbox target Companion is selected explicitly by the user from the
  Companion page. The control is a toggle: switching it on selects the target,
  and switching it off cancels the target selection.
- The Companion is selected for system float ball only when it is paired, online,
  configured as the System Inbox target, and declares `system_float_ball` or a
  namespaced variant such as `system_float_ball.macos`.
- The web UI always exposes the attention inbox from the navigation bar. The old
  fixed page-level floating bubble is not used as a fallback.
- The navigation inbox can request/stop the system float ball. A successful start
  response means the system helper has acknowledged that its window is visible.
- If no System Inbox target is configured, start returns `target_required` and the
  browser should navigate to `/companions?system_inbox=1`.
- Canceling the System Inbox target clears the target configuration and, when a
  helper is believed to be running, best-effort stops it on the previous target
  Companion.
- A successful system float ball start persists the target Companion's desired
  running state and the browser session id used by the helper. When the target
  Companion reconnects after an OpenFocus or Companion reload, OpenFocus restores
  the desired system float ball without requiring another browser click. An
  explicit stop clears that desired state.
- The browser navigation control and the system helper must reconcile against
  OpenFocus desired state instead of trusting only local UI memory. Helpers poll
  `/api/float_ball/state` and exit when their browser session is no longer the
  desired running system inbox.

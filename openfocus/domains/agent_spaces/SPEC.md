<!-- SPDX-License-Identifier: Apache-2.0 -->
# Agent Spaces Domain SPEC

## Responsibility

The Agent Spaces domain owns AgentSpace workspace lifecycle and terminal
ownership state:

- AgentSpace release use cases keyed by `Task.public_id`
- AgentSession start and terminate use cases keyed by `AgentSpace.id`
- explicit `TerminalOwner` values for AgentSpace and InspirationSpace terminals
- terminal listing, naming, lookup, rename, create, and local deletion
- deletion of terminal output rows tied to closed/released terminals
- Agent Session and Agent Message cleanup when an AgentSpace is released
- AgentSpace prompt catalog fields used by prompt zone display and auto injection

## Boundaries

- Domain code must not depend on FastAPI request/response objects or Jinja templates.
- Companion gRPC calls remain infrastructure glue. Routes pass a runtime resolver
  into the workspace release use case; the domain does not know the global
  Companion registry.
- AgentSession start/terminate use cases receive a small runtime port with
  `request_agent_start(...)` and `request_agent_terminate(...)`. The domain does
  not know `COMPANION_GRPC`, Companion registry lookup, HTTP status codes,
  request parsing, audit memory, SSE publish, or templates.
- AgentSession `/send`, message chunk persistence, prompt injection, and SSE
  streaming remain outside this domain boundary until that lifecycle is
  explicitly moved.
- AgentSpace prompt `enabled` controls prompt zone visibility.
- AgentSpace prompt `auto_enabled` controls automatic prompt concatenation on AgentSpace terminal input submit; it must not create runtime activity by itself.
- Built-in `send basic` injects the current Task `content` into the active terminal without submitting Enter and can participate in built-in auto prompt injection.
- When an AgentSpace is created with `start_agent_command`, only the automatically created default terminal should submit that command; user-created terminals must remain blank.
- AgentSpace UI settings are client-side presentation state. They may control files/preview/terminal font sizes and pane visibility without changing domain ownership or terminal lifecycle records.
- The AgentSpace fixed settings/prompt column owns task metadata display, pane visibility icons, Start Agent command settings, and workspace cleanup actions so the surrounding page can devote more space to files, preview, and terminal.

## Invariants

- AgentSpace terminal owner maps to `owner_type = "agent_space"` and `owner_id = AgentSpace.id`.
- Inspiration terminal owner maps to `owner_type = "inspiration_space"` and `owner_id = InspirationSpace.id`.
- `RemoteTerminalSession.space_id` is a legacy compatibility column only; new code must not use it for ownership checks.
- Terminal names are unique per owner.
- Closing/releasing a terminal removes its output rows as well as its session row.
- Auto prompts are applied only to active AgentSpace terminal input. They do not apply to Agent Session `/send` or Inspiration terminal `Summary` / `Create Goal` flows.
- Hiding an AgentSpace pane is layout-only and must not close terminals, release the AgentSpace, or mutate workspace files.
- The settings column cannot be hidden. When the terminal pane is hidden, prompt-zone controls are hidden but settings controls remain available.
- Releasing a missing AgentSpace is idempotent success.
- Starting an AgentSession calls the runtime before writing the local
  `AgentSession` row. If runtime start fails, OpenFocus must not leave a local
  dirty session row.
- Terminating an AgentSession first validates that the session belongs to the
  requested AgentSpace. Runtime terminate failure leaves the local
  `AgentSession.status` unchanged; web routes map Companion terminate failure to
  HTTP 502.
- Releasing an AgentSpace best-effort stops all remote terminals through the
  shared Terminals gateway and best-effort terminates managed AgentSessions on
  Companion, then deletes OpenFocus-side AgentSession, AgentMessage, terminal
  metadata/output, and AgentSpace records.
- If Companion is offline, terminal stop fails, or AgentSession terminate fails,
  release still completes local OpenFocus cleanup.
- Remote terminal stop happens before local terminal record deletion. If local
  cleanup fails, terminal records remain available for retry/reconciliation.

<!-- SPDX-License-Identifier: Apache-2.0 -->
# Agent Spaces Domain SPEC

## Responsibility

The Agent Spaces domain owns AgentSpace workspace lifecycle and terminal
ownership state:

- AgentSpace create/update/get use cases keyed by `Task.public_id`
- AgentSpace page read model composition for Task/Goal/AgentSpace/Companion
  data keyed by `Task.public_id`
- AgentSpace release use cases keyed by `Task.public_id`
- Start Agent command load/update use cases keyed by `AgentSpace.id`
- AgentSession start, send preflight/user-message persistence, assistant
  turn placeholder/failure persistence, runtime turn started/failed projection,
  terminate, read model listing, message listing, and SSE ownership validation
  use cases keyed by `AgentSpace.id`
- explicit `TerminalOwner` values for AgentSpace and InspirationSpace terminals
- terminal listing, naming, lookup, rename, create, and local deletion
- deletion of terminal output rows tied to closed/released terminals
- Agent Session and Agent Message cleanup when an AgentSpace is released
- AgentSpace prompt catalog fields used by prompt zone display and auto injection
- AgentSpace prompt catalog list, management ordering, create/update, enabled
  toggles, auto-enabled toggles, deletion, normalization, validation, and stable
  web-adapter payloads
- AgentSpace Prompt Master optimize use case, which validates an existing
  AgentSpace, calls the configured OpenFocus LLM provider, and returns rewritten
  prompt text without mutating prompt catalog state
- AgentSpace web/API adapters expose workspace file list/read/raw and file path
  list endpoints through the Companion domain file helpers

## Boundaries

- Domain code must not depend on FastAPI request/response objects or Jinja templates.
- AgentSpace create/get and Start Agent command persistence belongs to this
  domain. The domain normalizes task/space identifiers, validates command
  length, verifies local Task/Companion/AgentSpace state, and returns stable
  results for web adapters.
- Web routes still own request parsing, HTTP error mapping, templates, and
  Companion live/online registry checks. The AgentSpace page route also owns
  request-specific base URL/query-derived values such as `agent_prefix` and
  `auto_start_agent_command`; Task/Goal/AgentSpace/Companion page data comes
  from this domain read model. AgentSpace creation only validates local paired
  Companion state in this domain.
- Companion gRPC calls remain infrastructure glue. Routes pass a runtime resolver
  into the workspace release use case; the domain does not know the global
  Companion registry.
- AgentSpace file path cache traversal and cache invalidation belong to the
  Companion domain. AgentSpace routes only parse HTTP query parameters and map
  Companion domain errors to HTTP responses.
- AgentSession start/terminate use cases receive a small runtime port with
  `request_agent_start(...)` and `request_agent_terminate(...)`. The domain does
  not know `COMPANION_GRPC`, Companion registry lookup, HTTP status codes,
  request parsing, audit memory, SSE publish, or templates.
- AgentSession `/send` local state belongs to this domain: validate a
  non-empty `session_id`, verify the session belongs to the requested
  `AgentSpace.id`, read the session context needed by runtime send, persist the
  user `AgentMessage`, generate the request id returned to the web adapter,
  create the assistant placeholder, and project `runtime.turn.started`.
- Runtime send failure local state belongs to this domain: mark the assistant
  `AgentMessage` done/error and project `runtime.turn.failed`. The web adapter
  still maps the runtime failure to HTTP/SSE responses.
- AgentSession runtime send, web/request prompt assembly, SSE publishing, audit
  memory, and HTTP error mapping remain in the web/infrastructure adapter.
  AgentSession list/messages payload shape, session id normalization, ownership
  validation, and ordering belong to this domain. SSE transport, subscription,
  unsubscribe, heartbeat, and event framing remain in web/infrastructure.
  Streaming chunk transport and async assistant chunk append/finalize handling
  still live in infrastructure until that lifecycle gets a dedicated domain use
  case.
- AgentSpace prompt `enabled` controls prompt zone visibility.
- AgentSpace prompt `auto_enabled` controls automatic prompt concatenation on AgentSpace terminal input submit; it must not create runtime activity by itself.
- AgentSpace prompt catalog create/update trims `title` and `content`, requires
  both after trim, limits title to 160 characters and content to 20000
  characters, reports missing prompts as a domain error for update/toggle, and
  treats deleting a missing prompt as idempotent success.
- Prompt Master optimize trims input, requires a non-empty `prompt`, limits it
  to 20000 characters, maps missing AgentSpace/task to not found, and maps LLM
  provider/config/call failures to adapter-level upstream errors. It rewrites
  only the user's draft into fluent text in the draft's primary language and
  must not add Task, Goal, repo, AgentSpace, IDs, numbered requirements, or
  implementation details that were not present in the draft.
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
- Prompt Master optimize must not inject text into a terminal, submit Enter,
  start an AgentSession, write terminal input history, or create/update/delete
  `AgentSpacePrompt` records. It is a pure request/response optimization action
  from the user's perspective.
- Hiding an AgentSpace pane is layout-only and must not close terminals, release the AgentSpace, or mutate workspace files.
- The settings column cannot be hidden. When the terminal pane is hidden, prompt-zone controls are hidden but settings controls remain available.
- AgentSpace creation is an upsert by `Task.public_id`; creating a space for a
  task that already has one updates `companion_id`, `root_path`, `agent_type`,
  and `start_agent_command` instead of creating a duplicate row.
- Start Agent commands are stored trimmed and must be at most 2000 characters
  after trimming.
- Looking up a missing AgentSpace by `Task.public_id` returns an explicit
  missing-space result for adapters to map without treating it as an error.
- Loading the AgentSpace page read model trims `Task.public_id`, raises
  `AgentSpaceTaskNotFound` with `Task not found` when the Task is missing, and
  returns `space=None`/`companion=None` when the Task exists without an
  AgentSpace.
- Releasing a missing AgentSpace is idempotent success.
- Starting an AgentSession calls the runtime before writing the local
  `AgentSession` row. If runtime start fails, OpenFocus must not leave a local
  dirty session row.
- Terminating an AgentSession first validates that the session belongs to the
  requested AgentSpace. Runtime terminate failure leaves the local
  `AgentSession.status` unchanged; web routes map Companion terminate failure to
  HTTP 502.
- Sending an AgentSession message first validates that the session id is
  non-empty and belongs to the requested AgentSpace, then persists the user
  message and returns a generated request id plus runtime-send context. It does
  not call Companion runtime or publish SSE.
- AgentSession read use cases trim `session_id`, reject empty ids as validation
  errors, report missing or wrong-space sessions as not found, list sessions by
  newest persisted row first, and list messages by oldest persisted row first.
- SSE ownership validation uses the AgentSession domain read interface and
  returns the normalized session id that web/infrastructure uses for subscribe,
  publish, and unsubscribe operations.
- Beginning an AgentSession assistant turn persists an empty assistant
  `AgentMessage` with `role=assistant`, the generated request id, and
  `done=False`, then projects `runtime.turn.started` through an injected or
  default runtime-turn projector.
- Failing an AgentSession assistant turn after runtime send failure marks the
  assistant message `done=True`, stores the error text, and projects
  `runtime.turn.failed`.
- Releasing an AgentSpace best-effort stops all remote terminals through the
  shared Terminals gateway and best-effort terminates managed AgentSessions on
  Companion, then deletes OpenFocus-side AgentSession, AgentMessage, terminal
  metadata/output, and AgentSpace records.
- If Companion is offline, terminal stop fails, or AgentSession terminate fails,
  release still completes local OpenFocus cleanup.
- Remote terminal stop happens before local terminal record deletion. If local
  cleanup fails, terminal records remain available for retry/reconciliation.

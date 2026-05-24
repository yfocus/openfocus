<!-- SPDX-License-Identifier: Apache-2.0 -->
# Black-Box Regression Suite SPEC

## Responsibility

The black-box regression suite protects OpenFocus user workflows during core
refactors. It exercises the running FastAPI application through public HTTP
routes and API responses, not domain services, repositories, or private helper
functions.

## Boundaries

- Tests must use the same isolated SQLite setup as the normal pytest suite.
- Tests should not inspect database rows directly.
- Tests should derive follow-up operation IDs from public responses, rendered
  pages, redirects, or documented API payloads.
- External LLM providers and real Companion processes are out of scope for this
  suite; fallback planner behavior and control-plane HTTP flows are in scope.
- AgentSpace backend black-box coverage may use an in-process Companion
  command-port fake for the external data-plane dependency, but OpenFocus
  behavior must still be driven through public HTTP routes and API responses.

## Invariants Covered

- Goal and Task title/content are created and edited through user-facing routes.
- External Agent/Skill completion reports create review evidence but never mark
  Tasks as done.
- Task completion and reopening require explicit human actions.
- Recent Events, Calendar, and Next Move feedback stay connected to the same
  Task public ID.
- Inspiration spaces do not create Goals/Tasks before a user-confirmed publish.
- Inspiration publish creates only user-selected Tasks and records unselected
  draft Tasks as deferred in the publish result and Published Summary.
- Published Inspiration spaces are read-only and can only continue via fork.
- AgentSpace backend journey creates an AgentSpace through public Task APIs,
  lists/previews workspace files through the paired Companion, creates a remote
  terminal, injects command, Prompt Zone, and PREVIEW file-reference payloads,
  verifies terminal history through public APIs, closes the terminal, and
  releases the AgentSpace without browser automation.

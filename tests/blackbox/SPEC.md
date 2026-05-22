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

## Invariants Covered

- Goal and Task title/content are created and edited through user-facing routes.
- External Agent/Skill completion reports create review evidence but never mark
  Tasks as done.
- Task completion and reopening require explicit human actions.
- Recent Events, Calendar, and Next Move feedback stay connected to the same
  Task public ID.
- Inspiration spaces do not create Goals/Tasks before a user-confirmed publish.
- Published Inspiration spaces are read-only and can only continue via fork.

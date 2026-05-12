<!-- SPDX-License-Identifier: Apache-2.0 -->
# Agent Spaces Domain SPEC

## Responsibility

The Agent Spaces domain owns terminal ownership and local terminal lifecycle state:

- explicit `TerminalOwner` values for AgentSpace and InspirationSpace terminals
- terminal listing, naming, lookup, rename, create, and local deletion
- deletion of terminal output rows tied to closed/released terminals

## Boundaries

- Domain code must not depend on FastAPI request/response objects or Jinja templates.
- Companion gRPC calls remain infrastructure glue and are invoked by routes for now.
- Routes pass owner information and Companion results into the domain service.

## Invariants

- AgentSpace terminal owner maps to `owner_type = "agent_space"` and `owner_id = AgentSpace.id`.
- Inspiration terminal owner maps to `owner_type = "inspiration_space"` and `owner_id = InspirationSpace.id`.
- `RemoteTerminalSession.space_id` is a legacy compatibility column only; new code must not use it for ownership checks.
- Terminal names are unique per owner.
- Closing/releasing a terminal removes its output rows as well as its session row.

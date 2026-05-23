<!-- SPDX-License-Identifier: Apache-2.0 -->
# Inspirations Domain SPEC

## Responsibility

The Inspirations domain owns the ideation and planning workspace lifecycle:

- create, update, close, reopen, fork, and publish `InspirationSpace` records
- manage resources stored under each inspiration workspace `resources/` directory
- persist planner messages, draft versions, and publish records
- bridge Bring Your Own Agent terminal output into synchronized resources and structured drafts
- publish human-confirmed drafts into Goals/Tasks through the Goals domain service

## Boundaries

- Inspiration terminals are owned through the Agent Spaces terminal owner seam; they must not fake a Task or AgentSpace.
- External terminal agents are untrusted collaborators. They may create files in the inspiration workspace, but cannot directly create Goals/Tasks or mark an inspiration as published.
- Draft generation and publish confirmation remain OpenFocus-controlled workflows.
- Publishing must call the Goals domain service so Goal/Task defaults, events, and audit behavior stay consistent.
- Domain code must not depend on FastAPI request/response objects or Jinja templates.

## Invariants

- Every `InspirationSpace` has a stable workspace with a `resources/` directory.
- User-visible resources must have a readable file representation in that workspace.
- `resources/draft_summary.md` is a terminal-agent bridge input named `Summary`, not a published archive.
- `Published Summary` is generated after successful publish from the final confirmed draft and selected tasks.
- No Goal/Task rows are written before the user confirms `Publish`.
- Published inspiration spaces are read-only and cannot be reopened.


## External Interface

- `workspace.py` owns Inspiration workspace read models for list/detail/page context, lifecycle state transitions for close/reopen/delete, and resource use cases for create/update/replace/delete/raw/sync.
- Web routes call the workspace interface and translate domain errors into HTTP responses; routes must not rebuild list/detail counts, latest draft aggregation, resource state-machine checks, ORM mutations, raw file existence checks, or resource file cleanup/sync ordering.
- Lifecycle results may instruct the web adapter to perform asynchronous terminal release, but the state transition and related row/file cleanup stay in the domain.
- Resource upload bytes may be read by the Web adapter, but domain resource use cases receive only stored file metadata and never import FastAPI upload/response types.

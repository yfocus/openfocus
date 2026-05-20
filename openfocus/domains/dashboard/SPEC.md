<!-- SPDX-License-Identifier: Apache-2.0 -->
# Dashboard Domain SPEC

## Responsibility

The Dashboard domain owns read-side projections for `GET /goals`:

- load the Goal list and Task groups used by the three-column Dashboard
- derive display-only Goal/Task titles without writing them back to storage
- aggregate Task and Goal event snippets with user-facing labels
- derive Task UI status from confirmed Task state plus current runtime activity
- resolve the selected Goal/Task from query parameters

## Boundaries

- Dashboard is a read model. It must not create, update, finish, reopen, or delete Goals/Tasks.
- Goal/Task writes belong to the Goals domain service.
- Event writes and human-readable event formatting belong to the Events domain.
- Current runtime activity belongs to `openfocus/domains/agent_activity/`; Dashboard only reads its projection.
- Web routes own HTTP request/response objects and template rendering.

## Invariants

- External Agent/Skill completion reports stay pending confirmation and never become `done` through this read model.
- `Task.status == "done"` is the only completed Task source of truth.
- Runtime activity can make an unfinished Task display as `in_progress`, but cannot mutate `Task.status`.
- Goal and Task `title`/`content` remain the original user semantic fields; truncation is display-only.

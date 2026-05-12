<!-- SPDX-License-Identifier: Apache-2.0 -->
# Goals Domain SPEC

## Responsibility

The Goals domain owns Goal and Task write-side business rules:

- create, update, finish, reopen, and delete goals
- create, update, finish, reopen, and delete tasks
- derive task metadata (`task_type`, `estimated_minutes`, `context_key`)
- write Goal/Task domain events
- write Memory audit entries for Goal/Task changes

## Boundaries

- Domain code must not depend on FastAPI request/response objects or Jinja templates.
- Routes pass validated protocol inputs into this service and translate domain errors into HTTP responses.
- Direct Goal/Task writes from other domains should go through this service so defaults, events, and audit behavior stay consistent.

## Invariants

- External Agent/Skill reports never directly mark a task done; completion is a user-confirmed domain operation.
- Every created task belongs to an existing goal.
- Goal/Task writes must keep event and audit side effects consistent.
- Deleting a task removes its local AgentSpace persistence records.

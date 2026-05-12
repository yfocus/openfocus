<!-- SPDX-License-Identifier: Apache-2.0 -->
# Recommendations Domain SPEC

## Responsibility

The Recommendations domain owns Next Move generation and user feedback persistence:

- invoke the selected recommendation agent
- persist recommendation runs
- expose the latest recommendation payload shape
- persist feedback and feedback-learning memory notes

## Boundaries

- Domain code must not depend on FastAPI request/response objects or Jinja templates.
- Routes provide provider configuration and translate domain errors into HTTP responses.
- Recommendation candidates must come from existing open tasks; agents must not create tasks.

## Invariants

- Feedback is persisted to `next_move_feedback` before returning success.
- Dismissal feedback writes an event and audit memory entry.
- Recommendation output is capped by the route-provided limit, with an upper bound of 3.

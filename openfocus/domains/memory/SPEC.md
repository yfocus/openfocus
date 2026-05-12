<!-- SPDX-License-Identifier: Apache-2.0 -->
# Memory Domain SPEC

## Responsibility

The Memory domain owns file-based workspace memory persistence:

- audit memory append and rotation
- audit-to-daily summaries
- daily finalization
- long-term memory extraction and writes
- safe file listing and reading for the Memory page

## Boundaries

- Domain code must not depend on FastAPI, Jinja templates, SQLAlchemy sessions, or route request/response objects.
- Routes may call `service.py` to assemble page context and trigger maintenance.
- Filesystem operations are isolated in `filesystem.py`.

## Invariants

- Audit files are summarized before rotation creates the next current audit file.
- Daily memory files are retained permanently by domain policy.
- Audit retention only deletes expired audit files; it must not delete daily or long-term memory.
- Long-term memory contains only stable facts, preferences, or long-lived constraints.

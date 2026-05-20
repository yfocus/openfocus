<!-- SPDX-License-Identifier: Apache-2.0 -->
# Schema migration strategy

OpenFocus keeps Alembic as the canonical schema history for production-grade database upgrades, while the startup migration runner remains a compatibility repair path for existing local SQLite databases and tests. New durable schema changes should be represented by Alembic revisions; startup migrations may only contain idempotent repairs needed to keep older local databases bootable until they can be migrated cleanly.

**Consequences**

- `openfocus/infrastructure/alembic/versions/` is the source of truth for schema history.
- `openfocus/infrastructure/migrations.py` must stay small, idempotent, and compatibility-focused.
- Tests should continue to cover both paths until startup repair is no longer needed.

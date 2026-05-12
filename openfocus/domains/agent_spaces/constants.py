# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

TERMINAL_OWNER_AGENT_SPACE = "agent_space"
TERMINAL_OWNER_INSPIRATION_SPACE = "inspiration_space"
TERMINAL_STATUS_ACTIVE = "active"
TERMINAL_STATUS_CLOSED = "closed"
TERMINAL_STATUSES = frozenset({TERMINAL_STATUS_ACTIVE, TERMINAL_STATUS_CLOSED})

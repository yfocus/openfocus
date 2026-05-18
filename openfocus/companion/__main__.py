# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import sys

from .runtime import _print_banner, run_companion, send_protocol_url

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--bind-url":
        raise SystemExit(0 if send_protocol_url(sys.argv[2]) else 1)
    _print_banner()
    try:
        asyncio.run(run_companion())
    except KeyboardInterrupt:
        raise SystemExit(0)

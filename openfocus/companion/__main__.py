# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio

from .runtime import _print_banner, run_companion

if __name__ == "__main__":
    _print_banner()
    try:
        asyncio.run(run_companion())
    except KeyboardInterrupt:
        raise SystemExit(0)

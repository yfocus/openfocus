# SPDX-License-Identifier: Apache-2.0
"""Check that tracked source files carry the Apache-2.0 SPDX header."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SPDX = "SPDX-License-Identifier: Apache-2.0"

SUPPORTED_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".md",
    ".proto",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".svg",
    ".yaml",
    ".yml",
}

SUPPORTED_NAMES = {
    ".env-default",
    ".gitignore",
    "Makefile",
}

EXCLUDED_PREFIXES = ("openfocus/static/dist/",)

EXCLUDED_SUFFIXES = (
    ".jpeg",
    ".jpg",
    ".map",
    ".png",
    ".pyc",
)

EXCLUDED_NAMES = {
    "LICENSE",
}


def git_files(root: Path) -> list[str]:
    output = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        text=True,
    )
    return [line for line in output.splitlines() if line]


def should_check(path: str) -> bool:
    file_path = Path(path)
    if file_path.name in EXCLUDED_NAMES:
        return False
    if path.startswith(EXCLUDED_PREFIXES):
        return False
    if file_path.suffix in EXCLUDED_SUFFIXES:
        return False
    return file_path.name in SUPPORTED_NAMES or file_path.suffix in SUPPORTED_SUFFIXES


def has_spdx_header(root: Path, path: str) -> bool:
    try:
        text = (root / path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return True
    return SPDX in text[:1000]


def main() -> int:
    root = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
    )
    missing = [
        path
        for path in git_files(root)
        if should_check(path) and not has_spdx_header(root, path)
    ]

    if not missing:
        return 0

    print("The following source files are missing the Apache-2.0 SPDX header:")
    for path in missing:
        print(f"  - {path}")
    print(f"\nAdd `{SPDX}` using the file's native comment syntax.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Fail CI on high-confidence credential material in repository files."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

PATTERNS = {
    "coinglass_key": re.compile(rb"\bcg_[A-Za-z0-9_-]{24,}\b"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "bearer_token": re.compile(rb"(?i)authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~-]{16,}"),
    "assigned_secret": re.compile(rb"(?i)(?:api[_-]?key|password|secret|token)[ \t]*=[ \t]*['\"]?[A-Za-z0-9+/=_-]{24,}"),
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".db", ".sqlite", ".pyc"}


def candidates(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw]


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for path in candidates(root):
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES or path.name == "uv.lock":
            continue
        content = path.read_bytes()
        for name, pattern in PATTERNS.items():
            if pattern.search(content):
                failures.append(f"{path.relative_to(root)}: {name}")
    if failures:
        raise SystemExit("potential secrets found:\n" + "\n".join(failures))
    print("secret scan passed")


if __name__ == "__main__":
    main()

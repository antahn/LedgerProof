"""Fail if a real credential appears in the working tree or in git history.

Written after a live webhook signing secret reached a committed artifact: the
`stripe listen` banner prints the secret, and the artifact was captured by
tailing that log. Reviewing the diff was not enough, so this is a check rather
than a habit.

Test fixtures deliberately contain fake secrets. They are distinguished by
SHAPE, not by an allowlist of paths: a real Stripe signing secret is 64 hex
chars and a real key is a long base62 string, while the fixtures use short or
obviously patterned values. Anything matching the real shape fails, wherever it
lives — including a test file, where it would not belong either.

    uv run python scripts/scan_secrets.py           # working tree
    uv run python scripts/scan_secrets.py --history # every commit too
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PATTERNS: dict[str, re.Pattern[str]] = {
    "stripe live secret key": re.compile(r"sk_live_[A-Za-z0-9]{16,}"),
    "stripe test secret key": re.compile(r"sk_test_[A-Za-z0-9]{24,}"),
    "stripe restricted key": re.compile(r"rk_(live|test)_[A-Za-z0-9]{24,}"),
    "webhook signing secret": re.compile(r"whsec_[A-Za-z0-9]{40,}"),
    "anthropic api key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
}


def _run(*argv: str) -> str:
    return subprocess.run(
        argv, cwd=REPO_ROOT, capture_output=True, text=True, check=False
    ).stdout


def scan_working_tree() -> list[str]:
    hits: list[str] = []
    files = [f for f in _run("git", "ls-files").splitlines() if f]
    for name, pattern in PATTERNS.items():
        for rel in files:
            path = REPO_ROOT / rel
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{rel}:{lineno}: {name}")
    return hits


def scan_history() -> list[str]:
    revs = [r for r in _run("git", "rev-list", "--all").splitlines() if r]
    if not revs:
        return []
    hits: list[str] = []
    for name, pattern in PATTERNS.items():
        out = _run("git", "grep", "-nE", pattern.pattern, *revs)
        for line in out.splitlines():
            if line.strip():
                hits.append(f"{line}  <- {name}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", action="store_true", help="scan every commit, not just HEAD")
    args = ap.parse_args()

    hits = scan_working_tree()
    where = "working tree"
    if args.history:
        hits += scan_history()
        where = "working tree and git history"

    if hits:
        print(f"SECRET SCAN FAILED — {len(hits)} match(es) in {where}:", file=sys.stderr)
        for hit in hits[:50]:
            print(f"  {hit}", file=sys.stderr)
        print(
            "\nA real credential must never be committed. Redact it, then scrub history "
            "(git filter-branch / git-filter-repo) before pushing anywhere.",
            file=sys.stderr,
        )
        return 1

    print(f"secret scan clean ({where})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

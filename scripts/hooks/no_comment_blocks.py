#!/usr/bin/env python3
"""Reject multi-line comment blocks in newly ADDED Python lines.

House rule: a backend comment is one line, and only for a constraint the
code cannot show; rationale belongs in the commit message. Only added
lines are judged, so touching a legacy file does not inherit its
comments. A block whose first line ends in ``(keep)`` is allowed, for the
rare table or diagram that needs the room.
"""

from __future__ import annotations

import re
import subprocess
import sys

_MAX_RUN = 2
_KEEP = re.compile(r"\(keep\)\s*$")
_DIVIDER = re.compile(r"^\s*#\s*[─=—-]{3,}")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(path: str) -> list[tuple[int, str]]:
    """(line number, text) for every line this commit adds to *path*."""
    try:
        diff = subprocess.run(
            ["git", "diff", "--cached", "-U0", "--", path],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return []
    out: list[tuple[int, str]] = []
    lineno = 0
    for line in diff.splitlines():
        hunk = _HUNK.match(line)
        if hunk:
            lineno = int(hunk.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            out.append((lineno, line[1:]))
            lineno += 1
    return out


def offenders(added: list[tuple[int, str]]) -> list[tuple[int, int]]:
    blocks: list[tuple[int, int]] = []
    start = prev = None
    run = 0
    keep = False
    for lineno, text in added:
        stripped = text.strip()
        is_comment = stripped.startswith("#") and not _DIVIDER.match(text)
        contiguous = prev is not None and lineno == prev + 1
        if is_comment and (start is None or contiguous):
            if start is None:
                start, run, keep = lineno, 1, bool(_KEEP.search(text))
            else:
                run += 1
            prev = lineno
            continue
        if start is not None and run > _MAX_RUN and not keep:
            blocks.append((start, run))
        if is_comment:
            start, run, keep, prev = lineno, 1, bool(_KEEP.search(text)), lineno
        else:
            start, run, keep, prev = None, 0, False, lineno
    if start is not None and run > _MAX_RUN and not keep:
        blocks.append((start, run))
    return blocks


def main(paths: list[str]) -> int:
    failed = False
    for path in paths:
        if not path.endswith(".py"):
            continue
        for start, length in offenders(added_lines(path)):
            failed = True
            print(
                f"{path}:{start}: {length} new consecutive comment lines "
                f"(max {_MAX_RUN}). Say it in one line, or put the "
                f"rationale in the commit message."
            )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

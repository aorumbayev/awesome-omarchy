#!/usr/bin/env python3
"""Check or fix alphabetical order of README resource lists."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ITEM_RE = re.compile(r"^- \[([^\]]+)\]\(([^)]+)\)")
SKIP_HEADINGS = frozenset({"Contents"})
DEFAULT_README = Path("README.md")


def heading_name(line: str) -> str | None:
    if line.startswith("## "):
        return line[3:].strip()
    return None


def sort_key(line: str) -> tuple[str, str]:
    match = ITEM_RE.match(line)
    if not match:
        return ("", "")
    return (match.group(1).casefold(), match.group(2).casefold())


def ordered_lines(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    problems: list[str] = []
    heading: str | None = None
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.rstrip("\n")
        name = heading_name(stripped)
        if name is not None:
            heading = name
            out.append(raw)
            i += 1
            continue
        if heading in SKIP_HEADINGS or not ITEM_RE.match(stripped):
            out.append(raw)
            i += 1
            continue

        block: list[str] = []
        while i < len(lines) and ITEM_RE.match(lines[i].rstrip("\n")):
            block.append(lines[i])
            i += 1
        sorted_block = sorted(block, key=sort_key)
        if sorted_block != block:
            for actual, wanted in zip(block, sorted_block, strict=True):
                if actual != wanted:
                    want = ITEM_RE.match(wanted.rstrip("\n"))
                    got = ITEM_RE.match(actual.rstrip("\n"))
                    problems.append(
                        f"{heading}: [{got.group(1) if got else '?'}] should be "
                        f"[{want.group(1) if want else '?'}]"
                    )
                    break
        out.extend(sorted_block)

    result = "".join(out)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("readme", nargs="?", type=Path, default=DEFAULT_README)
    parser.add_argument("--fix", action="store_true", help="rewrite the file in sorted order")
    args = parser.parse_args()

    text = args.readme.read_text()
    sorted_text, problems = ordered_lines(text)

    if args.fix:
        if sorted_text != text:
            args.readme.write_text(sorted_text)
            print(f"sorted lists in {args.readme}")
        else:
            print(f"{args.readme} already sorted")
        return 0

    if problems:
        print(f"{args.readme} lists are not alphabetical:", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        print("\nFix with: python3 scripts/check-list-order.py --fix README.md", file=sys.stderr)
        return 1

    print(f"{args.readme} lists are alphabetical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Fail if newly added GitHub README listings have fewer than 5 stars."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ITEM_RE = re.compile(r"^- \[([^\]]+)\]\(([^)]+)\)")
GH_REPO_RE = re.compile(
    r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?(?:\.git)?$",
    re.IGNORECASE,
)
SKIP_OWNERS = frozenset({"topics", "orgs", "marketplace", "settings", "github"})
MIN_STARS = 5
DEFAULT_README = Path("README.md")


def git_ok(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def resolve_base(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    base_ref = os.environ.get("GITHUB_BASE_REF")
    if base_ref:
        return f"origin/{base_ref}"
    for ref in ("origin/main", "origin/master", "main", "master"):
        if git_ok(["rev-parse", "--verify", ref]).returncode == 0:
            return ref
    return None


def listing_repos(line: str) -> str | None:
    item = ITEM_RE.match(line.strip())
    if not item:
        return None
    match = GH_REPO_RE.match(item.group(2).strip())
    if not match:
        return None
    owner, name = match.group(1), match.group(2)
    if name.endswith(".git"):
        name = name[:-4]
    if owner.lower() in SKIP_OWNERS:
        return None
    return f"{owner}/{name}"


def repos_from(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        slug = listing_repos(line)
        if slug:
            found.setdefault(slug.lower(), slug)
    return found


def added_repos(base: str, readme: Path) -> list[str]:
    shown = git_ok(["show", f"{base}:{readme.as_posix()}"])
    old_text = shown.stdout if shown.returncode == 0 else ""
    old = repos_from(old_text)
    current = repos_from(readme.read_text())
    return [current[key] for key in current.keys() - old.keys()]


def star_counts(slugs: list[str]) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    for offset in range(0, len(slugs), 40):
        batch = slugs[offset : offset + 40]
        parts = []
        for i, slug in enumerate(batch):
            owner, name = slug.split("/", 1)
            parts.append(
                f'r{i}: repository(owner:{json.dumps(owner)}, name:{json.dumps(name)})'
                "{stargazerCount}"
            )
        payload = json.dumps({"query": "query{" + " ".join(parts) + "}"})
        result = subprocess.run(
            ["gh", "api", "graphql", "--input", "-"],
            input=payload,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            for slug in batch:
                counts[slug] = None
            continue
        data = (json.loads(result.stdout).get("data") or {})
        for i, slug in enumerate(batch):
            repo = data.get(f"r{i}")
            counts[slug] = None if not repo else int(repo["stargazerCount"])
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("readme", nargs="?", type=Path, default=DEFAULT_README)
    parser.add_argument("--base", help="git ref to diff against (default: origin/main)")
    parser.add_argument("--min-stars", type=int, default=MIN_STARS)
    args = parser.parse_args()

    base = resolve_base(args.base)
    if base is None:
        print("no git base ref; skipping star check")
        return 0

    repos = added_repos(base, args.readme)
    if not repos:
        print("no new GitHub listings")
        return 0

    counts = star_counts(repos)
    failures: list[str] = []
    for slug in repos:
        stars = counts.get(slug)
        if stars is None:
            failures.append(f"{slug}: not a public GitHub repository")
            continue
        if stars < args.min_stars:
            failures.append(f"{slug}: {stars} stars (need {args.min_stars}+)")

    if failures:
        print("new GitHub listings below the star bar:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1

    print(f"checked {len(repos)} new GitHub listing(s); all have {args.min_stars}+ stars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

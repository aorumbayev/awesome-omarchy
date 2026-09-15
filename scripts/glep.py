#!/usr/bin/env python3
"""Glep: start README-only fork CI, post the merge bar, nag leftovers, never Approve."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse

MARKER = "<!-- glep-merge-bar -->"
CHECK_NAME = "merge-bar"
README = "README.md"
ALLOWED_WORKFLOW_PATHS = frozenset(
    {
        ".github/workflows/awesome.yml",
        ".github/workflows/links.yml",
    }
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
MENTION_RE = re.compile(r"@[\w-]+")
BASE_BRANCHES = frozenset({"main", "master"})
MAINTAINER = os.environ.get("MAINTAINER", "aorumbayev")
REQUIRED_CHECKS = (
    "awesome-lint",
    "format-check",
    "spell-check",
    "list-order",
    "min-stars",
)
LINK_CHECK = "link-check"
URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+")
LOG_PREFIX = re.compile(r"^[^\t]+\t[^\t]+\t\S+\s")
TRAIL = ".,;:)]}>\"'"


@dataclass
class Leftover:
    key: str
    line: str


@dataclass
class Bar:
    green: bool
    leftovers: list[Leftover]
    preexisting_dead: list[str] = field(default_factory=list)
    ci_done: bool = True
    readme_only: bool = True
    summary: str = ""


def repo() -> str:
    value = os.environ.get("GITHUB_REPOSITORY")
    if not value:
        raise SystemExit("GITHUB_REPOSITORY is not set")
    return value


def dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}


def gh(
    args: list[str],
    *,
    token: str | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    result = subprocess.run(
        ["gh", *args],
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"gh {' '.join(args)} failed: {err}")
    return result


def app_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN is not set")
    return token


def check_token() -> str:
    return os.environ.get("CHECK_TOKEN") or app_token()


def api(
    path: str,
    *,
    method: str = "GET",
    token: str | None = None,
    fields: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    raw: bool = False,
    check: bool = True,
) -> Any:
    if query:
        path = path + ("&" if "?" in path else "?") + urlencode(query)
    args = ["api", "-X", method, path]
    if raw:
        args.extend(["-H", "Accept: application/vnd.github.raw"])
    if method != "GET":
        args.append("--input")
        result = gh(args, token=token, input_text=json.dumps(fields or {}), check=check)
    else:
        result = gh(args, token=token, check=check)
    if raw:
        return result.stdout
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def paginate(path: str, *, token: str | None = None) -> list[Any]:
    result = gh(["api", "--paginate", path], token=token)
    text = result.stdout.strip()
    if not text:
        return []
    chunks = json.loads(text)
    if isinstance(chunks, list) and chunks and isinstance(chunks[0], list):
        out: list[Any] = []
        for chunk in chunks:
            out.extend(chunk)
        return out
    if isinstance(chunks, list):
        return chunks
    return [chunks]


def normalize_url(raw: str) -> str:
    url = raw.rstrip(TRAIL)
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return url


def urls_in(text: str) -> set[str]:
    found: set[str] = set()
    for match in URL_RE.findall(text):
        found.add(normalize_url(match))
    return {url for url in found if url}


def readme_at(ref: str) -> str:
    if ref not in {"main", "master"}:
        ref = require_sha(ref)
    return str(
        api(
            f"repos/{repo()}/contents/{README}?ref={ref}",
            raw=True,
        )
    )


def pull(number: int) -> dict[str, Any]:
    return api(f"repos/{repo()}/pulls/{number}")


def require_sha(value: str) -> str:
    sha = (value or "").strip().lower()
    if not SHA_RE.fullmatch(sha):
        raise SystemExit(f"refusing untrusted sha: {value!r}")
    return sha


def require_pr_number(value: str) -> int:
    text = (value or "").strip()
    if not text.isdigit():
        raise SystemExit(f"refusing untrusted PR number: {value!r}")
    number = int(text)
    if number < 1:
        raise SystemExit(f"refusing untrusted PR number: {value!r}")
    return number


def require_branch(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if not text or len(text) > 250 or not BRANCH_RE.fullmatch(text):
        return None
    return text


def github_login(value: str) -> str | None:
    text = (value or "").strip()
    if LOGIN_RE.fullmatch(text):
        return text
    return None


def safe_text(text: str, limit: int = 500) -> str:
    cleaned = MENTION_RE.sub(lambda match: "(at)" + match.group(0)[1:], text)
    cleaned = cleaned.replace("<!--", "").replace("-->", "")
    return cleaned[:limit]


def safe_detail(text: str) -> str:
    return " ".join(safe_text(text, 180).split())


def readme_only_snapshot(number: int) -> tuple[bool, str]:
    pr = pull(number)
    sha = require_sha(pr["head"]["sha"])
    files = paginate(f"repos/{repo()}/pulls/{number}/files")
    if len(files) != 1:
        return False, sha
    item = files[0]
    name = item.get("filename") or ""
    previous = item.get("previous_filename") or ""
    if name != README or "/" in name:
        return False, sha
    if previous and previous != README:
        return False, sha
    return True, sha


def is_readme_only(number: int) -> bool:
    ok, _sha = readme_only_snapshot(number)
    return ok


def github_html_url(url: str) -> str:
    expected = f"https://github.com/{repo()}/"
    if (url or "").startswith(expected):
        return url
    return ""


def find_pr_for_sha(sha: str, head_repo: str | None, branch: str | None) -> int | None:
    sha = require_sha(sha)
    branch = require_branch(branch)
    if head_repo and branch and REPO_SLUG_RE.fullmatch(head_repo):
        owner = head_repo.split("/", 1)[0]
        pulls = api(
            f"repos/{repo()}/pulls",
            query={"state": "open", "head": f"{owner}:{branch}"},
        )
        for item in pulls or []:
            if (item.get("head") or {}).get("sha") == sha:
                return int(item["number"])
    commits = gh(
        ["api", f"repos/{repo()}/commits/{sha}/pulls"],
        check=False,
    )
    if commits.returncode == 0 and commits.stdout.strip():
        for item in json.loads(commits.stdout):
            if item.get("state") == "open" and (item.get("head") or {}).get("sha") == sha:
                return int(item["number"])
    return None


def waiting_runs(sha: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    for status in ("waiting", "pending", "requested"):
        payload = api(
            f"repos/{repo()}/actions/runs?event=pull_request&status={status}&per_page=100"
        )
        for run in payload.get("workflow_runs") or []:
            run_id = int(run["id"])
            if run_id in seen:
                continue
            if run.get("head_sha") != sha:
                continue
            if run.get("event") != "pull_request":
                continue
            if run.get("path") not in ALLOWED_WORKFLOW_PATHS:
                continue
            seen.add(run_id)
            found.append(run)
    return found


def approve_run(run_id: int) -> None:
    if dry_run():
        print(f"dry-run: approve workflow run {run_id}")
        return
    result = gh(
        ["api", "-X", "POST", f"repos/{repo()}/actions/runs/{run_id}/approve"],
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        if "already" in err.lower() or result.returncode == 1:
            print(f"approve {run_id}: {err}")
            return
        raise RuntimeError(f"approve {run_id} failed: {err}")
    print(f"approved workflow run {run_id}")


def approve_readme_only(number: int) -> bool:
    pr = pull(number)
    if (pr.get("base") or {}).get("ref") not in BASE_BRANCHES:
        print(f"PR #{number} base is not main; not approving runs")
        return False
    ok, sha = readme_only_snapshot(number)
    if not ok:
        print(f"PR #{number} changes more than {README}; not approving runs")
        return False
    approved = False
    for _ in range(12):
        ok, now = readme_only_snapshot(number)
        if not ok:
            print(f"PR #{number} changed files while waiting; skip approve")
            return False
        if now != sha:
            print(f"PR #{number} head moved; following {now[:12]}")
            sha = now
            continue
        runs = waiting_runs(sha)
        if not runs:
            if approved:
                return True
            time.sleep(5)
            continue
        for run in runs:
            ok, now = readme_only_snapshot(number)
            if not ok or now != sha:
                print(f"PR #{number} changed before approve; refuse")
                return False
            if run.get("path") not in ALLOWED_WORKFLOW_PATHS:
                print(f"refuse approve of {run.get('path')}")
                continue
            if run.get("head_sha") != sha:
                continue
            approve_run(int(run["id"]))
            approved = True
        time.sleep(2)
    if not approved:
        print(f"PR #{number}: no waiting workflow runs")
    return approved


def latest_checks(sha: str) -> dict[str, dict[str, Any]]:
    payload = api(f"repos/{repo()}/commits/{sha}/check-runs?per_page=100")
    latest: dict[str, dict[str, Any]] = {}
    for item in payload.get("check_runs") or []:
        name = item.get("name")
        if not name or name == CHECK_NAME:
            continue
        previous = latest.get(name)
        if previous is None or item["id"] > previous["id"]:
            latest[name] = item
    return latest


def check_done(item: dict[str, Any] | None) -> bool:
    return item is not None and item.get("status") == "completed"


def check_ok(item: dict[str, Any] | None) -> bool:
    return check_done(item) and item.get("conclusion") in {"success", "skipped", "neutral"}


def behind_main(sha: str) -> int:
    payload = api(f"repos/{repo()}/compare/main...{require_sha(sha)}")
    return int(payload.get("behind_by") or 0)


NOISE = (
    "hint:",
    "complete job name",
    "git switch",
    "detached",
    "safe directory",
    "npm warn",
    "found in cache",
    "post job cleanup",
    "cleaning up orphan",
    "temporarily overriding",
    "adding repository directory",
    "deprecationwarning",
    "node.js 20 is deprecated",
    "check-latest",
    "fetch_head",
    "from https://github.com",
    "shell:",
    "head is now at",
)
KEEP = (
    "should be",
    "star bar",
    "stars (",
    "need 5",
    "not alphabetical",
    "remark-lint",
    "fix with:",
    "readme.md:",
    "✖",
    "1 error",
)


def failed_log_lines(html_url: str) -> list[str]:
    match = re.search(r"/actions/runs/(\d+)/job/(\d+)", html_url or "")
    if not match:
        return []
    expected = f"https://github.com/{repo()}/actions/runs/"
    if not (html_url or "").startswith(expected):
        return []
    result = gh(
        ["run", "view", match.group(1), "--job", match.group(2), "--log-failed"],
        check=False,
    )
    if result.returncode != 0:
        return []
    useful: list[str] = []
    for raw in result.stdout.splitlines():
        line = LOG_PREFIX.sub("", raw).strip()
        line = re.sub(r"^\x1b\[[0-9;]*m", "", line)
        if not line or line.startswith("##[") or line.startswith("[command]"):
            continue
        lower = line.lower()
        if any(needle in lower for needle in NOISE):
            continue
        if any(needle in lower for needle in KEEP):
            useful.append(line)
    return useful[:8]


def pick_detail(lines: list[str]) -> str:
    if not lines:
        return ""
    ranked: list[tuple[int, str]] = []
    for line in lines:
        lower = line.lower()
        score = 0
        if "remark-lint" in lower:
            score += 7
        if re.search(r"readme\.md:\d+", lower):
            score += 3
        if "should be" in lower or "stars (" in lower:
            score += 5
        if "star bar" in lower or "not alphabetical" in lower:
            score += 2
        if lower in {"✖ linting", "- linting", "linting"}:
            score -= 4
        ranked.append((score, line))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def failed_urls_from_logs(html_url: str) -> set[str]:
    result_urls: set[str] = set()
    match = re.search(r"/actions/runs/(\d+)/job/(\d+)", html_url or "")
    if not match:
        return result_urls
    expected = f"https://github.com/{repo()}/actions/runs/"
    if not (html_url or "").startswith(expected):
        return result_urls
    result = gh(
        ["run", "view", match.group(1), "--job", match.group(2), "--log-failed"],
        check=False,
    )
    if result.returncode != 0:
        return result_urls
    for raw in result.stdout.splitlines():
        lower = raw.lower()
        if not any(tag in lower for tag in ("error", "fail", "✗", "[404]", "[410]", "broken")):
            continue
        result_urls.update(urls_in(raw))
    return result_urls


def compute_bar(number: int) -> Bar:
    pr = pull(number)
    sha = require_sha(pr["head"]["sha"])
    readme_only = is_readme_only(number)
    leftovers: list[Leftover] = []
    preexisting: list[str] = []

    behind = behind_main(sha)
    if behind:
        leftovers.append(
            Leftover(
                "behind",
                f"branch is {behind} commit(s) behind `main` — update it",
            )
        )

    checks = latest_checks(sha)
    needed = (*REQUIRED_CHECKS, LINK_CHECK)
    missing = [name for name in needed if not check_done(checks.get(name))]
    ci_done = not missing

    if missing and not readme_only:
        leftovers.append(
            Leftover(
                "not-readme-only",
                f"this PR changes more than `{README}`; a maintainer must approve workflows before CI can run",
            )
        )

    if missing and readme_only:
        leftovers.append(
            Leftover(
                "ci-pending:" + ",".join(missing),
                "CI has not finished yet (" + ", ".join(f"`{name}`" for name in missing) + ")",
            )
        )

    for name in REQUIRED_CHECKS:
        item = checks.get(name)
        if not check_done(item):
            continue
        if check_ok(item):
            continue
        html = github_html_url(item.get("html_url") or "")
        details = safe_detail(pick_detail(failed_log_lines(html)))
        extra = f" — {details}" if details else ""
        label = f"[`{name}`]({html})" if html else f"`{name}`"
        leftovers.append(
            Leftover(
                f"check:{name}",
                f"{label} failed{extra}",
            )
        )

    link = checks.get(LINK_CHECK)
    if check_done(link) and not check_ok(link):
        head_readme = readme_at(sha)
        base_readme = readme_at("main")
        added = urls_in(head_readme) - urls_in(base_readme)
        html = github_html_url(link.get("html_url") or "")
        failed = failed_urls_from_logs(html)
        author_dead = sorted(failed & added) if failed else []
        preexisting = sorted(failed - added) if failed else []
        if not failed and added:
            label = f"[`{LINK_CHECK}`]({html})" if html else f"`{LINK_CHECK}`"
            leftovers.append(
                Leftover(
                    "check:link-check",
                    f"{label} failed and Glep could not read which URLs died",
                )
            )
        for url in author_dead:
            leftovers.append(
                Leftover(
                    f"dead-url:{url}",
                    f"dead URL you added: {safe_text(url)}",
                )
            )
        if not author_dead and failed:
            print(f"PR #{number}: link-check is red only for URLs already on main")

    green = ci_done and not leftovers
    title = "green" if green else "blocked"
    summary_bits = [item.line for item in leftovers] or ["ready for maintainer review"]
    return Bar(
        green=green,
        leftovers=leftovers,
        preexisting_dead=preexisting,
        ci_done=ci_done,
        readme_only=readme_only,
        summary=f"Merge bar: {title}\n\n" + "\n".join(f"- {bit}" for bit in summary_bits),
    )


def leftover_key_blob(keys: list[str]) -> str:
    return ",".join(keys).replace("<!--", "").replace("-->", "")


def leftover_marker(keys: list[str]) -> str:
    return f"<!-- leftover:{leftover_key_blob(keys)} -->"


def comment_body(pr: dict[str, Any], bar: Bar, ping: bool) -> str:
    author = github_login(pr["user"]["login"])
    hello = f"@{author} " if ping and author else ""
    lines = [
        MARKER,
        f"{hello}Witch frog checking the merge bar.",
        "",
    ]
    if bar.green:
        lines += [
            "**Merge bar: green.**",
            "",
            "CI is honest. A human still decides if this listing belongs. Glep never Approves.",
        ]
    else:
        lines += [
            "**Merge bar: blocked.** This PR will not merge until the leftovers below are gone.",
            "",
        ]
        for item in bar.leftovers:
            lines.append(f"- [ ] {safe_text(item.line)}")
        lines += [
            "",
            "Glep never Approves the PR.",
        ]
    if bar.preexisting_dead:
        lines += [
            "",
            "Dead links already on `main` (not your job):",
        ]
        for url in bar.preexisting_dead:
            lines.append(f"- {safe_text(url)}")
    lines.append("")
    lines.append(leftover_marker([item.key for item in bar.leftovers]))
    return "\n".join(lines).rstrip() + "\n"


def leftover_keys_from_body(body: str) -> str:
    match = re.search(r"<!-- leftover:(.*) -->", body)
    return match.group(1) if match else ""


def find_sticky(number: int) -> dict[str, Any] | None:
    comments = paginate(f"repos/{repo()}/issues/{number}/comments")
    for item in reversed(comments):
        if MARKER in (item.get("body") or ""):
            return item
    return None


def upsert_comment(number: int, body: str, sticky: dict[str, Any] | None) -> None:
    if dry_run():
        print("dry-run comment:\n" + body)
        return
    if sticky:
        api(
            f"repos/{repo()}/issues/comments/{sticky['id']}",
            method="PATCH",
            fields={"body": body},
        )
        print(f"updated sticky comment on PR #{number}")
        return
    api(
        f"repos/{repo()}/issues/{number}/comments",
        method="POST",
        fields={"body": body},
    )
    print(f"posted sticky comment on PR #{number}")


def set_review_request(pr: dict[str, Any], want: bool) -> None:
    number = int(pr["number"])
    requested = {
        item["login"]
        for item in (pr.get("requested_reviewers") or [])
    }
    has = MAINTAINER in requested
    if want and has:
        return
    if not want and not has:
        return
    if dry_run():
        print(f"dry-run: {'request' if want else 'remove'} review from {MAINTAINER}")
        return
    path = f"repos/{repo()}/pulls/{number}/requested_reviewers"
    payload = {"reviewers": [MAINTAINER]}
    if want:
        api(path, method="POST", fields=payload)
        print(f"requested review from {MAINTAINER} on PR #{number}")
        return
    api(path, method="DELETE", fields=payload)
    print(f"removed review request for {MAINTAINER} on PR #{number}")


def upsert_check(sha: str, bar: Bar) -> None:
    token = check_token()
    payload = api(f"repos/{repo()}/commits/{sha}/check-runs?per_page=100", token=token)
    existing = None
    for item in payload.get("check_runs") or []:
        if item.get("name") == CHECK_NAME and item.get("head_sha") == sha:
            if existing is None or item["id"] > existing["id"]:
                existing = item
    if bar.ci_done:
        status = "completed"
        conclusion = "success" if bar.green else "failure"
        title = "Merge bar: green" if bar.green else "Merge bar: blocked"
    else:
        status = "in_progress"
        conclusion = None
        title = "Merge bar: waiting on CI"
    body: dict[str, Any] = {
        "name": CHECK_NAME,
        "head_sha": sha,
        "status": status,
        "output": {"title": title, "summary": bar.summary},
    }
    if conclusion:
        body["conclusion"] = conclusion
    if dry_run():
        print(f"dry-run check {status} {conclusion}: {title}")
        return
    if existing and existing.get("status") != "completed":
        api(
            f"repos/{repo()}/check-runs/{existing['id']}",
            method="PATCH",
            token=token,
            fields=body,
        )
    elif existing and status == "completed":
        api(
            f"repos/{repo()}/check-runs/{existing['id']}",
            method="PATCH",
            token=token,
            fields=body,
        )
    else:
        api(
            f"repos/{repo()}/check-runs",
            method="POST",
            token=token,
            fields=body,
        )
    print(f"posted {CHECK_NAME} on {sha[:12]}: {title}")


def gate(number: int) -> Bar:
    pr = pull(number)
    bar = compute_bar(number)
    upsert_check(require_sha(pr["head"]["sha"]), bar)
    sticky = find_sticky(number)
    only_waiting = bar.leftovers and all(
        item.key.startswith("ci-pending:") for item in bar.leftovers
    )
    if bar.green or not only_waiting:
        old_keys = leftover_keys_from_body(sticky["body"]) if sticky else None
        new_keys = leftover_key_blob([item.key for item in bar.leftovers])
        ping = old_keys != new_keys
        upsert_comment(number, comment_body(pr, bar, ping), sticky)
    want_review = bar.green and not pr.get("draft") and pr["user"]["login"] != MAINTAINER
    set_review_request(pr, want_review)
    return bar


def handle_pr(number: int) -> None:
    approve_readme_only(number)
    gate(number)


def backfill() -> None:
    pulls = paginate(f"repos/{repo()}/pulls?state=open")
    numbers = [int(item["number"]) for item in pulls]
    print(f"backfill {len(numbers)} open PR(s): {numbers}")
    for number in numbers:
        try:
            handle_pr(number)
        except Exception as exc:  # noqa: BLE001 — keep going across PRs
            print(f"PR #{number} failed: {exc}", file=sys.stderr)


def cmd_ci() -> None:
    event = os.environ.get("GLEP_EVENT", "")
    if event == "pull_request_target":
        handle_pr(require_pr_number(os.environ.get("GLEP_PR", "")))
        return
    if event == "workflow_run":
        source = os.environ.get("GLEP_SOURCE_WORKFLOW", "")
        if source not in ALLOWED_WORKFLOW_PATHS:
            raise SystemExit(f"refuse workflow_run for {source!r}")
        sha = require_sha(os.environ.get("GLEP_SHA", ""))
        number = find_pr_for_sha(
            sha,
            os.environ.get("GLEP_HEAD_REPO"),
            os.environ.get("GLEP_HEAD_BRANCH"),
        )
        if number is None:
            raise SystemExit(f"no open PR for {sha}")
        handle_pr(number)
        return
    if event == "workflow_dispatch":
        raw = (os.environ.get("GLEP_PR") or "").strip()
        if raw:
            handle_pr(require_pr_number(raw))
            return
        backfill()
        return
    raise SystemExit(f"unknown GLEP_EVENT: {event!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    pr_p = sub.add_parser("pr", help="approve README-only runs, then update the merge bar")
    pr_p.add_argument("number", type=int)
    approve_p = sub.add_parser(
        "approve", help="approve waiting runs if the PR only touches README.md"
    )
    approve_p.add_argument("number", type=int)
    gate_p = sub.add_parser("gate", help="compute merge bar, comment, request review")
    gate_p.add_argument("number", type=int)
    sha_p = sub.add_parser("sha", help="resolve a head SHA to a PR, then run pr")
    sha_p.add_argument("sha")
    sha_p.add_argument("--head-repo")
    sha_p.add_argument("--branch")
    sub.add_parser("backfill", help="run against every open PR")
    sub.add_parser("ci", help="entry point for GitHub Actions (reads GLEP_* env)")
    args = parser.parse_args()

    if args.cmd == "pr":
        handle_pr(args.number)
    elif args.cmd == "approve":
        approve_readme_only(args.number)
    elif args.cmd == "gate":
        gate(args.number)
    elif args.cmd == "sha":
        number = find_pr_for_sha(args.sha, args.head_repo, args.branch)
        if number is None:
            raise SystemExit(f"no open PR for {args.sha}")
        handle_pr(number)
    elif args.cmd == "backfill":
        backfill()
    elif args.cmd == "ci":
        cmd_ci()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

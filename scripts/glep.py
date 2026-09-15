#!/usr/bin/env python3
"""Glep starts CI when a fork pull request changes only README.md. Glep writes remaining items. Glep does not give approval."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

MARKER = "<!-- glep-merge-bar -->"
CHECK_NAME = "merge-bar"
README = "README.md"
ALLOWED_WORKFLOWS = frozenset(
    {".github/workflows/awesome.yml", ".github/workflows/links.yml"}
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
MENTION_RE = re.compile(r"@[\w-]+")
JOB_LOG_RE = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+")
LOG_PREFIX = re.compile(r"^[^\t]+\t[^\t]+\t\S+\s")
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
TRAIL = ".,;:)]}>\"'"
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


@dataclass(frozen=True)
class Leftover:
    key: str
    line: str


@dataclass
class Bar:
    leftovers: list[Leftover]
    preexisting_dead: list[str]
    ci_done: bool

    @property
    def green(self) -> bool:
        return self.ci_done and not self.leftovers

    @property
    def summary(self) -> str:
        title = "green" if self.green else "blocked"
        bits = [item.line for item in self.leftovers] or ["ready for maintainer review"]
        return f"Merge bar: {title}\n\n" + "\n".join(f"- {bit}" for bit in bits)


def repo() -> str:
    value = os.environ.get("GITHUB_REPOSITORY")
    if not value:
        raise SystemExit("GITHUB_REPOSITORY is not set")
    return value


def dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}


def app_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN is not set")
    return token


def check_token() -> str:
    return os.environ.get("CHECK_TOKEN") or app_token()


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
        path += ("&" if "?" in path else "?") + urlencode(query)
    args = ["api", "-X", method, path]
    if raw:
        args.extend(["-H", "Accept: application/vnd.github.raw"])
    result = (
        gh(args + ["--input"], token=token, input_text=json.dumps(fields or {}), check=check)
        if method != "GET"
        else gh(args, token=token, check=check)
    )
    if raw:
        return result.stdout
    return json.loads(result.stdout) if result.stdout.strip() else None


def paginate(path: str, *, token: str | None = None) -> list[Any]:
    text = gh(["api", "--paginate", path], token=token).stdout.strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, list) and data and isinstance(data[0], list):
        return [item for chunk in data for item in chunk]
    return data if isinstance(data, list) else [data]


def require_sha(value: str) -> str:
    sha = (value or "").strip().lower()
    if not SHA_RE.fullmatch(sha):
        raise SystemExit(f"refusing untrusted sha: {value!r}")
    return sha


def require_pr_number(value: str) -> int:
    text = (value or "").strip()
    if not text.isdigit() or (number := int(text)) < 1:
        raise SystemExit(f"refusing untrusted PR number: {value!r}")
    return number


def require_branch(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or len(text) > 250 or not BRANCH_RE.fullmatch(text):
        return None
    return text


def github_login(value: str) -> str | None:
    text = (value or "").strip()
    return text if LOGIN_RE.fullmatch(text) else None


def safe_text(text: str, limit: int = 500) -> str:
    cleaned = MENTION_RE.sub(lambda match: "(at)" + match.group(0)[1:], text)
    return cleaned.replace("<!--", "").replace("-->", "")[:limit]


def leftover_blob(keys: list[str]) -> str:
    return ",".join(keys).replace("<!--", "").replace("-->", "")


def urls_in(text: str) -> set[str]:
    return {url.rstrip(TRAIL) for url in URL_RE.findall(text) if url.rstrip(TRAIL)}


def github_html_url(url: str) -> str:
    expected = f"https://github.com/{repo()}/"
    return url if (url or "").startswith(expected) else ""


def pull(number: int) -> dict[str, Any]:
    return api(f"repos/{repo()}/pulls/{number}")


def readme_at(ref: str) -> str:
    if ref not in BASE_BRANCHES:
        ref = require_sha(ref)
    return str(api(f"repos/{repo()}/contents/{README}?ref={ref}", raw=True))


def readme_only_snapshot(number: int) -> tuple[bool, str]:
    pr = pull(number)
    sha = require_sha(pr["head"]["sha"])
    files = paginate(f"repos/{repo()}/pulls/{number}/files")
    if len(files) != 1:
        return False, sha
    item = files[0]
    name = item.get("filename") or ""
    previous = item.get("previous_filename") or ""
    if name != README or "/" in name or (previous and previous != README):
        return False, sha
    return True, sha


def find_pr_for_sha(sha: str, head_repo: str | None, branch: str | None) -> int | None:
    sha = require_sha(sha)
    branch = require_branch(branch)
    if head_repo and branch and REPO_SLUG_RE.fullmatch(head_repo):
        owner = head_repo.split("/", 1)[0]
        for item in api(
            f"repos/{repo()}/pulls",
            query={"state": "open", "head": f"{owner}:{branch}"},
        ) or []:
            if (item.get("head") or {}).get("sha") == sha:
                return int(item["number"])
    result = gh(["api", f"repos/{repo()}/commits/{sha}/pulls"], check=False)
    if result.returncode == 0 and result.stdout.strip():
        for item in json.loads(result.stdout):
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
            if (
                run_id in seen
                or run.get("head_sha") != sha
                or run.get("path") not in ALLOWED_WORKFLOWS
            ):
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
    if result.returncode == 0:
        print(f"approved workflow run {run_id}")
        return
    err = (result.stderr or result.stdout).strip()
    if "already" in err.lower():
        print(f"approve {run_id}: {err}")
        return
    raise RuntimeError(f"approve {run_id} failed: {err}")


def approve_readme_only(number: int) -> bool:
    if (pull(number).get("base") or {}).get("ref") not in BASE_BRANCHES:
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
            if run.get("path") not in ALLOWED_WORKFLOWS or run.get("head_sha") != sha:
                continue
            approve_run(int(run["id"]))
            approved = True
        time.sleep(2)
    if not approved:
        print(f"PR #{number}: no waiting workflow runs")
    return approved


def latest_checks(sha: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in api(f"repos/{repo()}/commits/{sha}/check-runs?per_page=100").get(
        "check_runs"
    ) or []:
        name = item.get("name")
        if name and name != CHECK_NAME and (name not in latest or item["id"] > latest[name]["id"]):
            latest[name] = item
    return latest


def check_done(item: dict[str, Any] | None) -> bool:
    return item is not None and item.get("status") == "completed"


def check_ok(item: dict[str, Any] | None) -> bool:
    return check_done(item) and item.get("conclusion") in {"success", "skipped", "neutral"}


def behind_main(sha: str) -> int:
    return int(api(f"repos/{repo()}/compare/main...{require_sha(sha)}").get("behind_by") or 0)


def failed_log(html_url: str) -> str:
    url = html_url or ""
    match = JOB_LOG_RE.search(url)
    if not match or not url.startswith(f"https://github.com/{repo()}/actions/runs/"):
        return ""
    result = gh(
        ["run", "view", match.group(1), "--job", match.group(2), "--log-failed"],
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def failed_log_lines(html_url: str) -> list[str]:
    useful: list[str] = []
    for raw in failed_log(html_url).splitlines():
        line = re.sub(r"^\x1b\[[0-9;]*m", "", LOG_PREFIX.sub("", raw).strip())
        if not line or line.startswith(("##[", "[command]")):
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

    def score(line: str) -> int:
        lower = line.lower()
        points = 0
        if "remark-lint" in lower:
            points += 7
        if re.search(r"readme\.md:\d+", lower):
            points += 3
        if "should be" in lower or "stars (" in lower:
            points += 5
        if "star bar" in lower or "not alphabetical" in lower:
            points += 2
        if lower in {"✖ linting", "- linting", "linting"}:
            points -= 4
        return points

    return max(lines, key=score)


def failed_urls_from_logs(html_url: str) -> set[str]:
    found: set[str] = set()
    for raw in failed_log(html_url).splitlines():
        lower = raw.lower()
        if any(tag in lower for tag in ("error", "fail", "✗", "[404]", "[410]", "broken")):
            found.update(urls_in(raw))
    return found


def compute_bar(number: int) -> Bar:
    readme_only, sha = readme_only_snapshot(number)
    leftovers: list[Leftover] = []
    preexisting: list[str] = []

    def add(key: str, line: str) -> None:
        leftovers.append(Leftover(key, line))

    behind = behind_main(sha)
    if behind:
        add("behind", f"branch is {behind} commit(s) behind `main` — update it")

    checks = latest_checks(sha)
    needed = (*REQUIRED_CHECKS, LINK_CHECK)
    missing = [name for name in needed if not check_done(checks.get(name))]
    ci_done = not missing

    if missing and not readme_only:
        add(
            "not-readme-only",
            f"this PR changes more than `{README}`; a maintainer must approve workflows before CI can run",
        )
    if missing and readme_only:
        add(
            "ci-pending:" + ",".join(missing),
            "CI has not finished yet (" + ", ".join(f"`{name}`" for name in missing) + ")",
        )

    for name in REQUIRED_CHECKS:
        item = checks.get(name)
        if not check_done(item) or check_ok(item):
            continue
        html = github_html_url(item.get("html_url") or "")
        details = " ".join(safe_text(pick_detail(failed_log_lines(html)), 180).split())
        extra = f" — {details}" if details else ""
        label = f"[`{name}`]({html})" if html else f"`{name}`"
        add(f"check:{name}", f"{label} failed{extra}")

    link = checks.get(LINK_CHECK)
    if check_done(link) and not check_ok(link):
        added = urls_in(readme_at(sha)) - urls_in(readme_at("main"))
        html = github_html_url(link.get("html_url") or "")
        failed = failed_urls_from_logs(html)
        author_dead = sorted(failed & added)
        preexisting = sorted(failed - added)
        if not failed and added:
            label = f"[`{LINK_CHECK}`]({html})" if html else f"`{LINK_CHECK}`"
            add("check:link-check", f"{label} failed and Glep could not read which URLs died")
        for url in author_dead:
            add(f"dead-url:{url}", f"dead URL you added: {safe_text(url)}")
        if failed and not author_dead:
            print(f"PR #{number}: link-check is red only for URLs already on main")

    return Bar(leftovers=leftovers, preexisting_dead=preexisting, ci_done=ci_done)


def comment_body(pr: dict[str, Any], bar: Bar, ping: bool) -> str:
    author = github_login(pr["user"]["login"])
    hello = f"@{author} " if ping and author else ""
    lines = [MARKER, f"{hello}Witch frog checking the merge bar.", ""]
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
            *[f"- [ ] {safe_text(item.line)}" for item in bar.leftovers],
            "",
            "Glep never Approves the PR.",
        ]
    if bar.preexisting_dead:
        lines += [
            "",
            "Dead links already on `main` (not your job):",
            *[f"- {safe_text(url)}" for url in bar.preexisting_dead],
        ]
    keys = leftover_blob([item.key for item in bar.leftovers])
    lines += ["", f"<!-- leftover:{keys} -->"]
    return "\n".join(lines).rstrip() + "\n"


def leftover_blob_from(body: str) -> str:
    match = re.search(r"<!-- leftover:(.*) -->", body)
    return match.group(1) if match else ""


def find_sticky(number: int) -> dict[str, Any] | None:
    for item in reversed(paginate(f"repos/{repo()}/issues/{number}/comments")):
        if MARKER in (item.get("body") or ""):
            return item
    return None


def upsert_comment(number: int, body: str, sticky: dict[str, Any] | None) -> None:
    if dry_run():
        print("dry-run comment:\n" + body)
        return
    if sticky:
        api(f"repos/{repo()}/issues/comments/{sticky['id']}", method="PATCH", fields={"body": body})
        print(f"updated sticky comment on PR #{number}")
        return
    api(f"repos/{repo()}/issues/{number}/comments", method="POST", fields={"body": body})
    print(f"posted sticky comment on PR #{number}")


def set_review_request(pr: dict[str, Any], want: bool) -> None:
    requested = {item["login"] for item in (pr.get("requested_reviewers") or [])}
    if want == (MAINTAINER in requested):
        return
    if dry_run():
        print(f"dry-run: {'request' if want else 'remove'} review from {MAINTAINER}")
        return
    api(
        f"repos/{repo()}/pulls/{int(pr['number'])}/requested_reviewers",
        method="POST" if want else "DELETE",
        fields={"reviewers": [MAINTAINER]},
    )
    verb = "requested review from" if want else "removed review request for"
    print(f"{verb} {MAINTAINER} on PR #{pr['number']}")


def upsert_check(sha: str, bar: Bar) -> None:
    token = check_token()
    existing = None
    for item in api(f"repos/{repo()}/commits/{sha}/check-runs?per_page=100", token=token).get(
        "check_runs"
    ) or []:
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
    if existing:
        api(f"repos/{repo()}/check-runs/{existing['id']}", method="PATCH", token=token, fields=body)
    else:
        api(f"repos/{repo()}/check-runs", method="POST", token=token, fields=body)
    print(f"posted {CHECK_NAME} on {sha[:12]}: {title}")


def gate(number: int) -> Bar:
    pr = pull(number)
    bar = compute_bar(number)
    upsert_check(require_sha(pr["head"]["sha"]), bar)
    sticky = find_sticky(number)
    only_waiting = bar.leftovers and all(item.key.startswith("ci-pending:") for item in bar.leftovers)
    if bar.green or not only_waiting:
        old = leftover_blob_from(sticky["body"]) if sticky else None
        new = leftover_blob([item.key for item in bar.leftovers])
        upsert_comment(number, comment_body(pr, bar, old != new), sticky)
    set_review_request(
        pr, bar.green and not pr.get("draft") and pr["user"]["login"] != MAINTAINER
    )
    return bar


def handle_pr(number: int) -> None:
    approve_readme_only(number)
    gate(number)


def backfill() -> None:
    numbers = [int(item["number"]) for item in paginate(f"repos/{repo()}/pulls?state=open")]
    print(f"backfill {len(numbers)} open PR(s): {numbers}")
    for number in numbers:
        try:
            handle_pr(number)
        except Exception as exc:  # noqa: BLE001
            print(f"PR #{number} failed: {exc}", file=sys.stderr)


def cmd_ci() -> None:
    event = os.environ.get("GLEP_EVENT", "")
    match event:
        case "pull_request_target":
            handle_pr(require_pr_number(os.environ.get("GLEP_PR", "")))
        case "workflow_run":
            source = os.environ.get("GLEP_SOURCE_WORKFLOW", "")
            if source not in ALLOWED_WORKFLOWS:
                raise SystemExit(f"refuse workflow_run for {source!r}")
            sha = require_sha(os.environ.get("GLEP_SHA", ""))
            number = find_pr_for_sha(
                sha, os.environ.get("GLEP_HEAD_REPO"), os.environ.get("GLEP_HEAD_BRANCH")
            )
            if number is None:
                raise SystemExit(f"no open PR for {sha}")
            handle_pr(number)
        case "workflow_dispatch":
            raw = (os.environ.get("GLEP_PR") or "").strip()
            handle_pr(require_pr_number(raw)) if raw else backfill()
        case _:
            raise SystemExit(f"unknown GLEP_EVENT: {event!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True)

    pr_p = sub.add_parser("pr", help="Start README-only CI. Then set the merge bar.")
    pr_p.add_argument("number", type=int)
    pr_p.set_defaults(run=lambda args: handle_pr(args.number))

    sub.add_parser("backfill", help="Operate on all open pull requests.").set_defaults(
        run=lambda _args: backfill()
    )
    sub.add_parser(
        "ci", help="Start from GitHub Actions. Read the GLEP_* environment variables."
    ).set_defaults(run=lambda _args: cmd_ci())

    args = parser.parse_args()
    args.run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

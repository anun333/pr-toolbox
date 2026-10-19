# SPDX-License-Identifier: GPL-3.0-or-later
"""PR health: what changed on your open PRs since the last look.

For each open PR of yours it records the CI result (leaving out Codecov and the summary job by
default), whether GitHub says it conflicts with master, review threads whose last word isn't
yours, the review decision, and, for drafts that wait on other PRs, whether those landed. It
reports only what changed since the previous run, including PRs that were merged or closed.

It only reads GitHub. Nothing is commented, labelled, re-run or pushed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .common import GITHUB_REPO, current_login, gh_json

QUERY = """query($q: String!, $cursor: String) { search(query: $q, type: ISSUE, first: 25, after: $cursor) {
  pageInfo { hasNextPage endCursor }
  nodes { ... on PullRequest { number title isDraft mergeable reviewDecision headRefOid url
    commits(last: 1) { nodes { commit { statusCheckRollup { state contexts(first: 100) { nodes {
      __typename ... on CheckRun { name conclusion status } ... on StatusContext { context state } } } } } } }
    reviewThreads(first: 100) { nodes { isResolved isOutdated comments(last: 1) { nodes { author { login } url } } } } } } } }"""

DEFAULT_IGNORE = r"^(codecov/.*|All Checks Passed)$"
FAILED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}
PENDING = {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}


def snapshot(author: str, ignore: re.Pattern) -> dict[str, dict]:
    prs, cursor = {}, None
    while True:
        args = ["api", "graphql", "-f", f"query={QUERY}", "-f", f"q=repo:{GITHUB_REPO} is:pr is:open author:{author}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        page = gh_json(*args)["data"]["search"]
        for node in page["nodes"]:
            if node:
                prs[str(node["number"])] = summarize(node, author, ignore)
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return prs


def summarize(node: dict, author: str, ignore: re.Pattern) -> dict:
    """One PR's state, reduced to what a change in would matter."""
    failing, pending, cancelled = [], 0, 0
    commits = node.get("commits", {}).get("nodes") or []
    rollup = (commits[0]["commit"].get("statusCheckRollup") or {}) if commits else {}
    for ctx in (rollup.get("contexts") or {}).get("nodes") or []:
        name = ctx.get("name") or ctx.get("context") or "?"
        if ignore.search(name):
            continue
        state = (ctx.get("conclusion") or ctx.get("state") or ctx.get("status") or "").upper()
        if state in FAILED:
            failing.append(name)
        elif state == "CANCELLED":
            cancelled += 1
        elif state in PENDING or (ctx.get("status") or "").upper() in PENDING:
            pending += 1
    waiting_people, waiting_bots = [], []
    for thread in (node.get("reviewThreads") or {}).get("nodes") or []:
        last = ((thread.get("comments") or {}).get("nodes") or [{}])[-1]
        who = (last.get("author") or {}).get("login", "")
        if thread.get("isResolved") or thread.get("isOutdated") or who == author:
            continue
        (waiting_bots if who.endswith("[bot]") or who in {"coderabbitai", "codecov"} else waiting_people).append(last.get("url", ""))
    ci = "failing" if failing else ("pending" if pending else ("passing" if commits else "none"))
    return {"title": node["title"], "url": node["url"], "draft": node["isDraft"], "head": node["headRefOid"],
            "mergeable": node.get("mergeable") or "UNKNOWN", "review": node.get("reviewDecision") or "",
            "ci": ci, "failing": sorted(set(failing)), "cancelled": cancelled,
            "waiting_people": waiting_people, "waiting_bots": waiting_bots}


def carry_mergeable(previous: dict, current: dict) -> None:
    """GitHub's search often answers UNKNOWN until it has computed mergeability; keep the last
    known answer rather than read UNKNOWN as a change."""
    for n, now in current.items():
        before = previous.get("prs", {}).get(n)
        if now["mergeable"] == "UNKNOWN" and before:
            now["mergeable"] = before["mergeable"]


def closed_state(number: str) -> str:
    pr = gh_json("api", f"repos/{GITHUB_REPO}/pulls/{number}")
    if pr.get("merged_at"):
        return f"merged by {(pr.get('merged_by') or {}).get('login', '?')}"
    return "closed" if pr.get("state") == "closed" else "open"


def events(previous: dict, current: dict, notes: dict) -> list[dict]:
    """What changed, one event per PR and kind."""
    out = []
    old = previous.get("prs", {})
    for n, now in current.items():
        before = old.get(n)
        if before is None:
            if old:
                out.append({"pr": n, "kind": "new", "text": "newly opened, now watched"})
            continue
        if now["ci"] == "failing" and (before["ci"] != "failing" or set(now["failing"]) - set(before["failing"])):
            out.append({"pr": n, "kind": "ci", "text": "CI failing: " + ", ".join(now["failing"])})
        elif now["ci"] == "passing" and before["ci"] == "failing":
            out.append({"pr": n, "kind": "ci", "text": "CI passing again"})
        if now["mergeable"] == "CONFLICTING" and before["mergeable"] != "CONFLICTING":
            out.append({"pr": n, "kind": "conflict", "text": "conflicts with master: needs a rebase"})
        elif now["mergeable"] == "MERGEABLE" and before["mergeable"] == "CONFLICTING":
            out.append({"pr": n, "kind": "conflict", "text": "merges with master again"})
        new_people = set(now["waiting_people"]) - set(before["waiting_people"])
        new_bots = set(now["waiting_bots"]) - set(before["waiting_bots"])
        if new_people:
            out.append({"pr": n, "kind": "review", "text": f"{len(new_people)} review thread(s) from people waiting on you", "urls": sorted(new_people)})
        if new_bots:
            out.append({"pr": n, "kind": "review", "text": f"{len(new_bots)} bot review thread(s) waiting on you", "urls": sorted(new_bots)})
        if now["review"] != before["review"] and now["review"] in {"APPROVED", "CHANGES_REQUESTED"}:
            out.append({"pr": n, "kind": "decision", "text": now["review"].lower().replace("_", " ")})
    merged = set()
    for n in old:
        if n not in current:
            state = closed_state(n)
            if state == "open":  # gone from search for a moment, not closed
                continue
            out.append({"pr": n, "kind": "closed", "text": state})
            if state.startswith("merged"):
                merged.add(n)
    for n, note in notes.items():
        waits = {str(d) for d in note.get("depends_on", [])}
        if n in current and waits & merged:
            remaining = sorted(waits & set(current), key=int)
            text = "dependency merged: " + ", ".join(f"#{d}" for d in sorted(waits & merged, key=int)) + "; rebase it"
            text += f" (still waiting on {', '.join(f'#{d}' for d in remaining)})" if remaining else (" and mark it ready" if current[n]["draft"] else "")
            out.append({"pr": n, "kind": "dependency", "text": text})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openemr-pr-tools health", description=__doc__.split("\n")[0])
    parser.add_argument("--author", help="GitHub login whose PRs to watch (default: the gh login)")
    parser.add_argument("--state-dir", default=".openemr-pr-tools", help="where the last snapshot is kept")
    parser.add_argument("--notes", help="per-PR notes with depends_on lists (default: <state-dir>/landing-order-notes.json)")
    parser.add_argument("--ignore", default=DEFAULT_IGNORE, help=f"regex of check names to leave out (default: {DEFAULT_IGNORE})")
    args = parser.parse_args(argv)
    state = Path(args.state_dir).expanduser()
    state.mkdir(parents=True, exist_ok=True)
    path = state / "pr-health.json"
    notes_path = Path(args.notes).expanduser() if args.notes else state / "landing-order-notes.json"
    previous = json.loads(path.read_text()) if path.exists() else {}
    notes = json.loads(notes_path.read_text()) if notes_path.exists() else {}
    author = args.author or current_login()
    current = snapshot(author, re.compile(args.ignore))
    carry_mergeable(previous, current)
    found = events(previous, current, notes)
    path.write_text(json.dumps({"checked": datetime.now(timezone.utc).isoformat(timespec="seconds"), "author": author, "prs": current}, indent=1) + "\n")
    print(json.dumps({"prs": len(current), "events": found}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

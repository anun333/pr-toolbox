# SPDX-License-Identifier: GPL-3.0-or-later
"""Landing order: which open PRs each of your OpenEMR PRs conflicts with.

Each open PR of yours is merged onto master in memory, then test-merged with every open PR that
changes any of the same files and still merges with master itself. The check reports textual
conflicts, the v_database each PR claims, and exact-cap collisions (two PRs that each pass
.phpstan/fatal-baseline-caps.php alone and fail it together). PRs that already conflict with master
are left out: they rebase before they can land, and what they conflict with after that can't be
known yet.

It reads GitHub and writes local files. Only --apply and --rewrite change anything on GitHub, and
then only the first paragraph of your own PRs' descriptions. Nothing is ever commented, labelled or
pinged, and a description changes only when its facts do, not because a date moved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .common import (CAPS_FILE, GITHUB_REPO, VERSION_FILES, caps_mismatches, current_login, fetch_prs, gh,
                     gh_json, git, is_version_conflict, merged, open_prs, repo_dir, v_database)

LINE_START = "**Landing order"


class Paths:
    def __init__(self, state_dir: str, notes: str | None, report: str | None):
        self.state = Path(state_dir).expanduser()
        self.state.mkdir(parents=True, exist_ok=True)
        self.facts = self.state / "landing-order.json"
        self.notes = Path(notes).expanduser() if notes else self.state / "landing-order-notes.json"
        self.report = Path(report).expanduser() if report else self.state / "landing-order.md"


def check(repo: Path, author: str, remote: str = "upstream") -> dict:
    prs = open_prs()
    by = {p["number"]: p for p in prs}
    ours = sorted((p for p in prs if p["author"] == author), key=lambda p: p["number"])
    sharing = {p["number"]: [o["number"] for o in prs if o["number"] != p["number"] and p["paths"] & o["paths"]] for p in ours}
    version_prs = [p["number"] for p in prs if "version.php" in p["paths"]]
    fetch_prs(repo, sorted({p["number"] for p in ours} | {n for ns in sharing.values() for n in ns} | set(version_prs)), remote)
    base = git(repo, "rev-parse", f"{remote}/master", check=True).stdout.strip()
    base_caps = set(caps_mismatches(repo, base))
    alone: dict[int, tuple[str | None, list[str]]] = {}

    def onto_master(n: int) -> tuple[str | None, list[str]]:
        if n not in alone:
            alone[n] = merged(repo, base, f"{remote}/pr/{n}")
        return alone[n]

    def new_caps(commit: str) -> set[str]:
        return set(caps_mismatches(repo, commit)) - base_caps

    facts: dict[str, dict] = {}
    for pr in ours:
        n = pr["number"]
        commit, master_conflicts = onto_master(n)
        entry = {"title": pr["title"], "draft": pr["isDraft"], "head": pr["headRefOid"],
                 "v_database": v_database(repo, f"{remote}/pr/{n}") if "version.php" in pr["paths"] else None,
                 "conflicts_with_master": master_conflicts, "conflicts": {}, "clean_with_ours": {}, "caps": [],
                 "has_line": (pr.get("body") or "").startswith(LINE_START)}
        if commit is not None:
            for o in sharing[n]:
                other, _ = onto_master(o)
                if other is None:
                    continue
                both, files = merged(repo, commit, f"{remote}/pr/{o}")
                if both is None:
                    entry["conflicts"][str(o)] = files
                    continue
                shared_source = sorted(f for f in (pr["paths"] & by[o]["paths"]) - VERSION_FILES if not f.startswith(".phpstan/"))
                if by[o]["author"] == author and shared_source:
                    entry["clean_with_ours"][str(o)] = shared_source[:3]
                if CAPS_FILE in pr["paths"] or CAPS_FILE in by[o]["paths"]:
                    if new_caps(both) and not new_caps(commit) and not new_caps(other):
                        entry["caps"].append(o)
        entry["key"] = fact_key(entry)
        facts[str(n)] = entry
    claims: dict[str, list[int]] = {}
    for n in version_prs:
        claim = v_database(repo, f"{remote}/pr/{n}")
        if claim is not None:
            claims.setdefault(str(claim), []).append(n)
    return {"checked": datetime.now(timezone.utc).isoformat(timespec="seconds"), "master": base, "author": author,
            "open_prs": len(prs), "ours": facts, "v_database_claims": dict(sorted(claims.items(), key=lambda kv: int(kv[0])))}


def fact_key(entry: dict) -> str:
    """What decides whether a PR's line changes: not its wording, and not the date."""
    facts = {k: entry[k] for k in ("v_database", "conflicts_with_master", "conflicts", "caps")}
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()[:12]


def sentence(n: str, entry: dict, result: dict, notes: dict) -> str:
    """The line for PR n. notes[n] may give a prefix and suffix, the PRs whose commits it carries
    (a stacked draft, which conflicts with them by construction), and wording for those cases."""
    note = notes.get(n, {})
    carries = {str(c) for c in note.get("carries", [])}
    built_on_it = {m for m, other in notes.items() if n in {str(c) for c in other.get("carries", [])}}
    stacked = carries | built_on_it
    conflicts = {o: f for o, f in entry["conflicts"].items() if o not in stacked}
    version = {o for o, f in conflicts.items() if is_version_conflict(f)}
    other = {o: f for o, f in conflicts.items() if o not in version}
    stamp = (f"test-merged on {result['checked'][:10]} against master {result['master'][:10]} and every open PR "
             "that changes any of the same files, the PHPStan baseline included")
    parts = [note.get("prefix") or f"{LINE_START}:**"]
    if not conflicts and not entry["caps"] and not carries:
        parts.append("independent. No open PR conflicts with it.")
    if note.get("carries_text"):
        parts.append(note["carries_text"])
    if version and note.get("sql_text"):
        parts.append(note["sql_text"])
    elif version:
        listed = ", ".join(f"#{o}" for o in sorted(version, key=int))
        parts.append(f"It adds to the upgrade script and takes `v_database` {entry['v_database']}. It conflicts there and in "
                     f"`version.php` with {listed}, so whichever lands second needs the next number; I'll renumber this one.")
    for o, files in sorted(other.items(), key=lambda kv: int(kv[0])):
        parts.append(f"It conflicts with #{o} in {', '.join(f'`{f}`' for f in files)}. Either can land first, and if #{o} does, I'll rebase this one.")
    for o in entry["caps"]:
        parts.append(f"With #{o} it would break an exact PHPStan cap that each passes alone; whichever lands second needs its cap re-counted.")
    for o, files in sorted(entry["clean_with_ours"].items(), key=lambda kv: int(kv[0])):
        if o not in stacked:
            parts.append(f"It shares {', '.join(f'`{f}`' for f in files)} with #{o}, and the two merge cleanly in either order.")
    if note.get("suffix"):
        parts.append(note["suffix"])
    return " ".join(parts) + f" ({stamp}.)"


def write_report(path: Path, result: dict, changed: list[str]) -> None:
    lines = ["# Landing order", "", f"Checked {result['checked']} against master `{result['master'][:10]}`: {result['open_prs']} open PRs, "
             f"those of {result['author']} compared with every PR that shares a file. PRs that already conflict with master are left out.", "",
             "| PR | v_database | Conflicts (with PRs that merge with master) | Cap collisions | Line |", "|---|---|---|---|---|"]
    for n, e in result["ours"].items():
        conflicts = "; ".join(f"#{o}: {', '.join(f)}" for o, f in e["conflicts"].items())
        conflicts = conflicts or ("**conflicts with master**" if e["conflicts_with_master"] else "none")
        state = "changed" if n in changed else ("yes" if e["has_line"] else "missing")
        lines.append(f"| #{n}{' (draft)' if e['draft'] else ''} | {e['v_database'] or ''} | {conflicts} | {', '.join(f'#{c}' for c in e['caps'])} | {state} |")
    claims = "; ".join(f"{v}: {', '.join(f'#{n}' for n in ns)}" for v, ns in result["v_database_claims"].items())
    lines += ["", f"v_database claims among open PRs: {claims or 'none'}", ""]
    path.write_text("\n".join(lines) + "\n")


def rewrite(numbers: list[str], result: dict, notes: dict, scratch: Path) -> list[str]:
    """Rewrites the first paragraph of each PR's description when it is a landing-order line, or
    adds one. Only the PRs named, which are expected to be the author's own."""
    done = []
    for n in numbers:
        body = gh("api", f"repos/{GITHUB_REPO}/pulls/{n}", "--jq", ".body")
        line = sentence(n, result["ours"][n], result, notes)
        if body.startswith(LINE_START):
            end = body.find("\n\n")
            new = line + (body[end:] if end != -1 else "")
        else:
            new = line + "\n\n" + body.lstrip("\n")
        if new == body:
            continue
        tmp = scratch / f".landing-{n}.md"
        tmp.write_text(new)
        try:
            gh("api", "-X", "PATCH", f"repos/{GITHUB_REPO}/pulls/{n}", "-F", f"body=@{tmp}", "--jq", ".number")
        finally:
            tmp.unlink(missing_ok=True)
        done.append(n)
    return done


def due(repo: Path, previous: dict, author: str, remote: str = "upstream") -> bool:
    """Whether master or one of the author's open PR heads moved since the last check."""
    if not previous or previous.get("author") != author:
        return True
    master = git(repo, "ls-remote", remote, "refs/heads/master").stdout.split()[:1]
    if master and master[0] != previous.get("master"):
        return True
    query = ("query($q: String!) { search(query: $q, type: ISSUE, first: 100) { nodes { ... on PullRequest { number headRefOid } } } }")
    found = gh_json("api", "graphql", "-f", f"query={query}", "-f", f"q=repo:{GITHUB_REPO} is:pr is:open author:{author}")
    heads = {str(node["number"]): node["headRefOid"] for node in found["data"]["search"]["nodes"] if node}
    return heads != {n: e["head"] for n, e in previous.get("ours", {}).items()}


def changes(previous: dict, result: dict) -> list[str]:
    """The author's PRs whose facts changed since the previous check (none on a first check)."""
    old = previous.get("ours", {})
    if not old:
        return []
    return [n for n, e in result["ours"].items() if n not in old or old[n]["key"] != e["key"]]


def describe_change(n: str, previous: dict, result: dict) -> str:
    entry, prev = result["ours"][n], previous.get("ours", {}).get(n, {})
    what = []
    if not prev:
        what.append("new PR, no facts before")
    added = sorted(set(entry["conflicts"]) - set(prev.get("conflicts", {})), key=int)
    gone = sorted(set(prev.get("conflicts", {})) - set(entry["conflicts"]), key=int)
    if added:
        what.append("now conflicts with " + ", ".join(f"#{o}" for o in added))
    if gone:
        what.append("no longer conflicts with " + ", ".join(f"#{o}" for o in gone))
    if prev and bool(entry["conflicts_with_master"]) != bool(prev.get("conflicts_with_master")):
        what.append("conflicts with master" if entry["conflicts_with_master"] else "merges with master again")
    if prev and entry["caps"] != prev.get("caps", []):
        what.append("cap collisions: " + (", ".join(f"#{o}" for o in entry["caps"]) or "none"))
    if prev and entry["v_database"] != prev.get("v_database"):
        what.append(f"v_database {prev.get('v_database')} -> {entry['v_database']}")
    return "; ".join(what) or "facts changed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openemr-pr-tools landing", description=__doc__.split("\n")[0])
    parser.add_argument("--author", help="GitHub login whose PRs to check (default: the gh login)")
    parser.add_argument("--repo", help="OpenEMR checkout (default: $OPENEMR_REPO or ~/dev/openemr)")
    parser.add_argument("--remote", default="upstream", help="remote that points at openemr/openemr (default: upstream)")
    parser.add_argument("--state-dir", default=".openemr-pr-tools", help="where the facts and report are kept")
    parser.add_argument("--notes", help="hand-written context per PR (default: <state-dir>/landing-order-notes.json)")
    parser.add_argument("--report", help="markdown report path (default: <state-dir>/landing-order.md)")
    parser.add_argument("--if-due", action="store_true", help="check only when master or one of the PRs moved")
    parser.add_argument("--apply", action="store_true", help="rewrite the lines of PRs whose facts changed")
    parser.add_argument("--show", nargs="+", metavar="N", help="print the line for these PRs from the last check")
    parser.add_argument("--rewrite", nargs="+", metavar="N", help="rewrite these PRs' lines from the last check")
    args = parser.parse_args(argv)
    paths = Paths(args.state_dir, args.notes, args.report)
    repo = repo_dir(args.repo)
    previous = json.loads(paths.facts.read_text()) if paths.facts.exists() else {}
    notes = json.loads(paths.notes.read_text()) if paths.notes.exists() else {}
    if args.show or args.rewrite:
        if not previous:
            print("no previous check; run without --show/--rewrite first", file=sys.stderr)
            return 2
        for n in args.show or []:
            print(f"#{n}: {sentence(n, previous['ours'][n], previous, notes)}\n")
        if args.rewrite:
            print(json.dumps({"rewritten": rewrite([n for n in args.rewrite if n in previous["ours"]], previous, notes, paths.state)}))
        return 0
    author = args.author or current_login()
    if args.if_due and not due(repo, previous, author, args.remote):
        print(json.dumps({"due": False}))
        return 0
    result = check(repo, author, args.remote)
    changed = changes(previous, result)
    missing = [n for n, e in result["ours"].items() if not e["has_line"]]
    applied = rewrite(sorted(set(changed) | set(missing), key=int), result, notes, paths.state) if args.apply else []
    for n in applied:
        result["ours"][n]["has_line"] = True
    paths.facts.write_text(json.dumps(result, indent=1) + "\n")
    write_report(paths.report, result, changed)
    print(json.dumps({"due": True, "master": result["master"][:10], "prs": len(result["ours"]),
                      "changed": [{"pr": n, "what": describe_change(n, previous, result)} for n in changed],
                      "missing_line": missing, "applied": applied}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

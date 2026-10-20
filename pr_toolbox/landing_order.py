# SPDX-License-Identifier: GPL-3.0-or-later
"""Landing order: which open PRs each of your PRs conflicts with.

Each open PR of yours is merged onto the base branch in memory, then test-merged with every open PR
that changes any of the same files and still merges with the base itself. The check reports textual
conflicts and, where the project profile defines them, version-number claims (a number every schema
change takes) and exact-cap collisions (two PRs that each pass a pinned count alone and fail it
together). PRs that already conflict with the base are left out: they rebase before they can land,
and what they conflict with after that can't be known yet.

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

from .common import (Target, add_target_arguments, caps_mismatches, current_login, fetch_prs, gh, gh_json, git,
                     is_version_conflict, matches, merged, open_prs, resolve, version_claim)

LINE_START = "**Landing order"


class Paths:
    def __init__(self, state_dir: str, notes: str | None, report: str | None):
        self.state = Path(state_dir).expanduser()
        self.state.mkdir(parents=True, exist_ok=True)
        self.facts = self.state / "landing-order.json"
        self.notes = Path(notes).expanduser() if notes else self.state / "landing-order-notes.json"
        self.report = Path(report).expanduser() if report else self.state / "landing-order.md"


def check(target: Target, author: str) -> dict:
    repo, base_ref = target.repo, target.base
    versions, caps_rule = target.config.get("version_claims"), target.config.get("exact_caps")
    noise = set((versions or {}).get("related", [])) | ({caps_rule["file"]} if caps_rule else set())
    prs = open_prs(target.github)
    by = {p["number"]: p for p in prs}
    ours = sorted((p for p in prs if p["author"] == author), key=lambda p: p["number"])
    sharing = {p["number"]: [o["number"] for o in prs if o["number"] != p["number"] and p["paths"] & o["paths"]] for p in ours}
    version_prs = [p["number"] for p in prs if versions and versions["file"] in p["paths"]]
    fetch_prs(target, sorted({p["number"] for p in ours} | {n for ns in sharing.values() for n in ns} | set(version_prs)))
    base = git(repo, "rev-parse", base_ref, check=True).stdout.strip()
    base_caps = set(caps_mismatches(repo, base, caps_rule))
    alone: dict[int, tuple[str | None, list[str]]] = {}

    def onto_base(n: int) -> tuple[str | None, list[str]]:
        if n not in alone:
            alone[n] = merged(repo, base, f"{target.remote}/pr/{n}")
        return alone[n]

    def new_caps(commit: str) -> set[str]:
        return set(caps_mismatches(repo, commit, caps_rule)) - base_caps

    facts: dict[str, dict] = {}
    for pr in ours:
        n = pr["number"]
        commit, base_conflicts = onto_base(n)
        entry = {"title": pr["title"], "draft": pr["isDraft"], "head": pr["headRefOid"],
                 "v_database": version_claim(repo, f"{target.remote}/pr/{n}", versions) if n in version_prs else None,
                 "conflicts_with_master": base_conflicts, "conflicts": {}, "clean_with_ours": {}, "caps": [],
                 "has_line": (pr.get("body") or "").startswith(LINE_START)}
        if commit is not None:
            for o in sharing[n]:
                other, _ = onto_base(o)
                if other is None:
                    continue
                both, files = merged(repo, commit, f"{target.remote}/pr/{o}")
                if both is None:
                    entry["conflicts"][str(o)] = files
                    continue
                shared = sorted(f for f in pr["paths"] & by[o]["paths"] if not matches(f, sorted(noise)) and not ignored(f, target.config))
                if by[o]["author"] == author and shared:
                    entry["clean_with_ours"][str(o)] = shared[:3]
                if caps_rule and (caps_rule["file"] in pr["paths"] or caps_rule["file"] in by[o]["paths"]):
                    if new_caps(both) and not new_caps(commit) and not new_caps(other):
                        entry["caps"].append(o)
        entry["key"] = fact_key(entry)
        facts[str(n)] = entry
    claims: dict[str, list[int]] = {}
    for n in version_prs:
        claim = version_claim(repo, f"{target.remote}/pr/{n}", versions)
        if claim is not None:
            claims.setdefault(str(claim), []).append(n)
    return {"checked": datetime.now(timezone.utc).isoformat(timespec="seconds"), "master": base, "base_branch": target.base_branch,
            "github": target.github, "author": author, "open_prs": len(prs), "ours": facts,
            "v_database_claims": dict(sorted(claims.items(), key=lambda kv: int(kv[0]))),
            "version_noun": (versions or {}).get("noun", "version number"), "version_file": (versions or {}).get("file", ""),
            "version_related": (versions or {}).get("related", []),
            "caps_noun": (caps_rule or {}).get("noun", "pinned count")}


def ignored(path: str, config: dict) -> bool:
    """Files that many PRs touch in passing (a baseline, generated docs), not worth naming as shared."""
    return matches(path, config.get("shared_ignore") or config.get("codespell_exclude") or [])


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
    rule = {"file": result["version_file"], "related": result.get("version_related") or [result["version_file"]]} if result.get("version_file") else None
    version = {o for o, f in conflicts.items() if is_version_conflict(f, rule)}
    other = {o: f for o, f in conflicts.items() if o not in version}
    stamp = (f"test-merged on {result['checked'][:10]} against {result.get('base_branch', 'master')} {result['master'][:10]} "
             "and every open PR that changes any of the same files")
    parts = [note.get("prefix") or f"{LINE_START}:**"]
    if not conflicts and not entry["caps"] and not carries:
        parts.append("independent. No open PR conflicts with it.")
    if note.get("carries_text"):
        parts.append(note["carries_text"])
    if version and note.get("sql_text"):
        parts.append(note["sql_text"])
    elif version:
        listed = ", ".join(f"#{o}" for o in sorted(version, key=int))
        noun, file = result.get("version_noun", "version number"), result.get("version_file", "")
        parts.append(f"It takes {noun} {entry['v_database']}, and it conflicts with {listed} only over that number (in `{file}` and the "
                     "files that go with it), so whichever lands second needs the next one; I'll renumber this one.")
    for o, files in sorted(other.items(), key=lambda kv: int(kv[0])):
        parts.append(f"It conflicts with #{o} in {', '.join(f'`{f}`' for f in files)}. Either can land first, and if #{o} does, I'll rebase this one.")
    for o in entry["caps"]:
        parts.append(f"With #{o} it would break an exact {result.get('caps_noun', 'pinned count')} that each passes alone; whichever lands second needs it re-counted.")
    for o, files in sorted(entry["clean_with_ours"].items(), key=lambda kv: int(kv[0])):
        if o not in stacked:
            parts.append(f"It shares {', '.join(f'`{f}`' for f in files)} with #{o}, and the two merge cleanly in either order.")
    if note.get("suffix"):
        parts.append(note["suffix"])
    return " ".join(parts) + f" ({stamp}.)"


def write_report(path: Path, result: dict, changed: list[str]) -> None:
    branch = result.get("base_branch", "master")
    lines = ["# Landing order", "", f"Checked {result['checked']} against {result.get('github', '')} {branch} `{result['master'][:10]}`: {result['open_prs']} open PRs, "
             f"those of {result['author']} compared with every PR that shares a file. PRs that already conflict with {branch} are left out.", "",
             f"| PR | {result.get('version_noun', 'version')} | Conflicts (with PRs that merge with {branch}) | Cap collisions | Line |", "|---|---|---|---|---|"]
    for n, e in result["ours"].items():
        conflicts = "; ".join(f"#{o}: {', '.join(f)}" for o, f in e["conflicts"].items())
        conflicts = conflicts or (f"**conflicts with {branch}**" if e["conflicts_with_master"] else "none")
        state = "changed" if n in changed else ("yes" if e["has_line"] else "missing")
        lines.append(f"| #{n}{' (draft)' if e['draft'] else ''} | {e['v_database'] or ''} | {conflicts} | {', '.join(f'#{c}' for c in e['caps'])} | {state} |")
    claims = "; ".join(f"{v}: {', '.join(f'#{n}' for n in ns)}" for v, ns in result["v_database_claims"].items())
    lines += ["", f"{result.get('version_noun', 'version number')} claims among open PRs: {claims or 'none'}", ""]
    path.write_text("\n".join(lines) + "\n")


def rewrite(numbers: list[str], result: dict, notes: dict, scratch: Path, github: str) -> list[str]:
    """Rewrites the first paragraph of each PR's description when it is a landing-order line, or
    adds one. Only the PRs named, which are expected to be the author's own."""
    done = []
    for n in numbers:
        body = gh("api", f"repos/{github}/pulls/{n}", "--jq", ".body")
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
            gh("api", "-X", "PATCH", f"repos/{github}/pulls/{n}", "-F", f"body=@{tmp}", "--jq", ".number")
        finally:
            tmp.unlink(missing_ok=True)
        done.append(n)
    return done


def due(target: Target, previous: dict, author: str) -> bool:
    """Whether the base branch or one of the author's open PR heads moved since the last check."""
    if not previous or previous.get("author") != author or previous.get("github", target.github) != target.github:
        return True
    head = git(target.repo, "ls-remote", target.remote, f"refs/heads/{target.base_branch}").stdout.split()[:1]
    if head and head[0] != previous.get("master"):
        return True
    query = "query($q: String!) { search(query: $q, type: ISSUE, first: 100) { nodes { ... on PullRequest { number headRefOid } } } }"
    found = gh_json("api", "graphql", "-f", f"query={query}", "-f", f"q=repo:{target.github} is:pr is:open author:{author}")
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
        what.append("conflicts with the base branch" if entry["conflicts_with_master"] else "merges with the base branch again")
    if prev and entry["caps"] != prev.get("caps", []):
        what.append("cap collisions: " + (", ".join(f"#{o}" for o in entry["caps"]) or "none"))
    if prev and entry["v_database"] != prev.get("v_database"):
        what.append(f"version claim {prev.get('v_database')} -> {entry['v_database']}")
    return "; ".join(what) or "facts changed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pr-toolbox landing", description=__doc__.split("\n")[0])
    add_target_arguments(parser)
    parser.add_argument("--author", help="GitHub login whose PRs to check (default: the gh login)")
    parser.add_argument("--state-dir", default=".pr-toolbox", help="where the facts and report are kept (default: .pr-toolbox)")
    parser.add_argument("--notes", help="hand-written context per PR (default: <state-dir>/landing-order-notes.json)")
    parser.add_argument("--report", help="markdown report path (default: <state-dir>/landing-order.md)")
    parser.add_argument("--if-due", action="store_true", help="check only when the base branch or one of the PRs moved")
    parser.add_argument("--apply", action="store_true", help="rewrite the lines of PRs whose facts changed")
    parser.add_argument("--show", nargs="+", metavar="N", help="print the line for these PRs from the last check")
    parser.add_argument("--rewrite", nargs="+", metavar="N", help="rewrite these PRs' lines from the last check")
    args = parser.parse_args(argv)
    paths = Paths(args.state_dir, args.notes, args.report)
    previous = json.loads(paths.facts.read_text()) if paths.facts.exists() else {}
    notes = json.loads(paths.notes.read_text()) if paths.notes.exists() else {}
    if args.show or args.rewrite:
        if not previous:
            print("no previous check; run without --show/--rewrite first", file=sys.stderr)
            return 2
        for n in args.show or []:
            print(f"#{n}: {sentence(n, previous['ours'][n], previous, notes)}\n")
        if args.rewrite:
            done = rewrite([n for n in args.rewrite if n in previous["ours"]], previous, notes, paths.state, previous["github"])
            print(json.dumps({"rewritten": done}))
        return 0
    target = resolve(args)
    author = args.author or current_login()
    if args.if_due and not due(target, previous, author):
        print(json.dumps({"due": False}))
        return 0
    result = check(target, author)
    changed = changes(previous, result)
    missing = [n for n, e in result["ours"].items() if not e["has_line"]]
    applied = rewrite(sorted(set(changed) | set(missing), key=int), result, notes, paths.state, target.github) if args.apply else []
    for n in applied:
        result["ours"][n]["has_line"] = True
    paths.facts.write_text(json.dumps(result, indent=1) + "\n")
    write_report(paths.report, result, changed)
    print(json.dumps({"due": True, "github": target.github, "base": f"{target.base_branch} {result['master'][:10]}", "prs": len(result["ours"]),
                      "changed": [{"pr": n, "what": describe_change(n, previous, result)} for n in changed],
                      "missing_line": missing, "applied": applied}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

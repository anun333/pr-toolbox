"""The conformance lens: which outside standards (certification criteria, rules) each open PR touches, read
from the profile's "conformance" object. A PR is matched by its files (path prefixes) or its title.

Controls run before anything is matched, and a failure stops the build, as a drifted quote does:
  - every path prefix must match at least one file on the base branch, so a typo can't hide a standard;
  - each control's files and title must match exactly the standards it expects, and a docs-only change none;
  - a control naming an open PR must match that PR as expected (one no longer open is reported, not failed).
"""

from __future__ import annotations

import re


def matcher(spec: dict):
    paths = tuple(spec.get("paths") or ())
    title_re = re.compile(spec["title_re"]) if spec.get("title_re") else None

    def why(files, title: str) -> list[str]:
        out = []
        if paths and any(f.startswith(paths) for f in files):
            out.append("files")
        if title_re and title and title_re.search(title):
            out.append("title")
        return out
    return why


def match_all(conf: dict, files, title: str) -> list[str]:
    """The ids of the standards a change with these files and this title touches, in the profile's order."""
    return [s["id"] for s in conf.get("specs", []) if matcher(s)(files, title)]


def check(conf: dict, base_files: list[str], open_prs: dict) -> tuple[list[str], list[str]]:
    """(failures, notes). open_prs maps a PR number to {"files": [...], "title": str} for every open PR."""
    failures, notes = [], []
    for s in conf.get("specs", []):
        for p in s.get("paths") or []:
            if not any(f.startswith(p) for f in base_files):
                failures.append(f"{s['id']}: no file on the base branch starts with {p}")
    for c in conf.get("controls", []):
        want = sorted(c.get("expect", []))
        if "pr" in c:
            pr = open_prs.get(c["pr"])
            if pr is None:
                notes.append(f"control #{c['pr']} is no longer open; replace it")
                continue
            got = sorted(match_all(conf, pr["files"], pr["title"]))
            name = f"#{c['pr']}"
        else:
            got = sorted(match_all(conf, c.get("files", []), c.get("title", "")))
            name = ", ".join(c.get("files", [])) or repr(c.get("title", ""))
        if got != want:
            failures.append(f"{name}: matched {got or 'nothing'}, expected {want or 'nothing'}")
    return failures, notes


def build(conf: dict, prs: dict, mass: set) -> tuple[list[dict], dict]:
    """(specs, conf_of): each spec with the open PRs that touch it, and each PR's matched spec ids.
    prs maps a PR number to {"files": [...], "title": str}; a PR over the mass limit is matched by title only."""
    conf_of: dict[int, list[str]] = {}
    out = []
    for s in conf.get("specs", []):
        why = matcher(s)
        inflight = []
        for n, pr in prs.items():
            reasons = why([] if n in mass else pr["files"], pr["title"])
            if reasons:
                inflight.append({"n": n, "why": reasons})
                conf_of.setdefault(n, []).append(s["id"])
        out.append({**{k: v for k, v in s.items() if k != "title_re"}, "inflight": sorted(inflight, key=lambda x: -x["n"])})
    return out, conf_of

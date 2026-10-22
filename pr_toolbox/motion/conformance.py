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


def ci_files(pattern: str | None, workflows: dict) -> list[str]:
    """The CI workflow files (path -> text) that run what the pattern names."""
    if not pattern:
        return []
    rx = re.compile(pattern)
    return sorted(f for f, text in workflows.items() if rx.search(text))


def names_found(names: list[str], text: str) -> set[str]:
    """The names that appear in the text as a whole token: quoted, or ending a path ('.../us-core-bmi')."""
    if not names:
        return set()
    rx = re.compile(r"[\"'/](" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")[\"']")
    return set(rx.findall(text))


def check(conf: dict, base_files: list[str], open_prs: dict, workflows: dict | None = None, name_text: str = "") -> tuple[list[str], list[str]]:
    """(failures, notes). open_prs maps a PR number to {"files": [...], "title": str} for every open PR; workflows maps
    each CI workflow file to its text; name_text is the code the names checks search."""
    failures, notes = [], []
    by_id = {s["id"]: s for s in conf.get("specs", [])}
    workflows = workflows or {}
    for s in conf.get("specs", []):
        for p in s.get("paths") or []:
            if not any(f.startswith(p) for f in base_files):
                failures.append(f"{s['id']}: no file on the base branch starts with {p}")
    for c in conf.get("controls", []):
        # the measured checks: each must be able to find, and to not find
        if "ci" in c or "ci_re" in c:
            pattern = by_id.get(c.get("ci"), {}).get("ci_re") if "ci" in c else c["ci_re"]
            got = ci_files(pattern, workflows)
            if (c["expect"] == "found") != bool(got) or ("expect_files" in c and got != sorted(c["expect_files"])):
                failures.append(f"CI control {c.get('ci') or c['ci_re']}: expected {c.get('expect_files') or c['expect']}, found {got or 'nothing'}")
            continue
        if "names" in c:
            spec_names = (by_id.get(c["names"], {}).get("names") or {}).get("names", [])
            found = names_found(spec_names + [c["planted"]], name_text)
            if c["planted"] in found or not found:
                failures.append(f"names control {c['names']}: the planted name was {'found' if c['planted'] in found else 'missing, but so was every real one'}")
            continue
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


def build(conf: dict, prs: dict, mass: set, workflows: dict | None = None, name_text: str = "", reported: dict | None = None,
          issue=None) -> tuple[list[dict], dict]:
    """(specs, conf_of): each spec with the open PRs that touch it, its measured and reported gaps and the state of its
    known ones, and each PR's matched spec ids. prs maps a PR number to {"files": [...], "title": str}; a PR over the
    mass limit is matched by title only. issue(n) returns a GitHub issue or PR record, for a known gap's state."""
    workflows, reported = workflows or {}, reported or {}
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
        measured = {"ci": ci_files(s.get("ci_re"), workflows)}
        if s.get("names"):
            want = s["names"]["names"]
            found = names_found(want, name_text)
            measured["names"] = {"label": s["names"]["label"], "source": s["names"].get("source"), "total": len(want),
                                 "found": len(found), "missing": sorted(set(want) - found)}
        known = []
        for k in s.get("known", []):
            m = re.search(r"/(?:issues|pull)/(\d+)", k.get("url") or "")
            rec = issue(int(m.group(1))) if (m and issue) else {}
            known.append({**k, "n": int(m.group(1)) if m else None, "state": rec.get("state"), "pr": rec.get("pr")})
        out.append({**{k: v for k, v in s.items() if k not in ("title_re", "names", "known")}, "inflight": sorted(inflight, key=lambda x: -x["n"]),
                    "measured": measured, "reported": reported.get(s["id"]), "known": known})
    return out, conf_of

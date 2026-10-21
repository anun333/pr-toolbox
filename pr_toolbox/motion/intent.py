# SPDX-License-Identifier: GPL-3.0-or-later
"""The work-in-motion map: what a project's maintainers plan for each area of the code (the profile's streams,
quoted from their own documents), which open PRs are changing it, what each release removed, and what merging any
open PR does to the others. Reads the profile, repo_audit.py's findings, and GitHub's issue and PR records; writes
map.json and the page.

    WIM_PROFILE=<profile.json> python3 -I intent.py <clone> <audit work dir> <cache dir> <page template> <page out>

Reads git objects and GitHub records as text; runs nothing from them.
"""
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date
from html import escape as html_escape
from pathlib import Path

REPO, W, G, TEMPLATE, OUT = (Path(a) for a in sys.argv[1:6])
HERE = Path(__file__).parent
P = json.loads(Path(os.environ["WIM_PROFILE"]).read_text())
MASTER = os.environ.get("WIM_MASTER") or f"{P['remote']}/{P['base_branch']}"
SKIP_LABELS = set(P["skip_labels"])
PLACEHOLDER_ISSUES = set(P["placeholder_issues"])  # e.g. a PR template's "Fixes #12345" example, left in many bodies
MECHANICAL = re.compile(P["mechanical"])
GH = re.escape(P["github"])


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).stdout


def release_tags():
    """The last N release tags matching the profile's pattern, oldest first, by their version numbers."""
    rx = re.compile(P["release_tags"]["pattern"])
    found = [(tuple(int(x) for x in m.groups()), t) for t in git("tag", "-l").split() if (m := rx.match(t))]
    return [t for _, t in sorted(found)[-P["release_tags"]["count"]:]]


TAGS = release_tags() + [MASTER]


def gh_issue(n):
    f = G / "issues" / f"{n}.json"
    if not f.exists():
        out = subprocess.run(["gh", "api", f"repos/{P['github']}/issues/{n}", "--jq",
                              "{number,title,state,user:.user.login,updated_at,labels:[.labels[].name],body,comments,pr:(.pull_request!=null)}"],
                             capture_output=True, text=True).stdout
        f.write_text(out or "{}")
    return json.loads(f.read_text() or "{}")


streams = P["streams"]
findings = json.loads((W / "findings.json").read_text())
prs_git = {int(k): v for k, v in json.loads((W / "prs.json").read_text()).items()}
meta = {p["number"]: p for p in json.loads((G / "pr-meta.json").read_text())}
bodies = {p["number"]: p for p in json.loads((G / "pr-bodies.json").read_text())}
extra = {int(k): v for k, v in json.loads((G / "pr-extra.json").read_text()).items()}
MAINTAINER_ROLES = set(P["maintainer_roles"])
BOT = re.compile(P["bots"])
mergers = json.loads((G / "mergers.json").read_text())
MAINTAINERS = {login for login in mergers if not BOT.search(login)}
is_maint = lambda login, role: not BOT.search(login) and (login in MAINTAINERS or role in MAINTAINER_ROLES)
DIRECTION = {d["id"]: {**d, "stream": s["id"]} for s in streams["streams"] for d in s.get("direction", [])}
master_sha = findings["summary"]["master"]
mass = {int(k): v for k, v in findings["summary"]["mass"].items()}
labels_of = {n: [l["name"] for l in meta[n].get("labels", [])] for n in meta}
skip = {n for n, ls in labels_of.items() if SKIP_LABELS & set(ls)}

# --- quotes: each target must appear in its source (issue body, or the file on master), else the build stops ----
def _norm(t):
    t = re.sub(r"(?m)^\s*\*\s?", "", t)  # PHP docblock prefixes
    # the same words in the same order; punctuation, markdown, list markers and spacing aside
    t = re.sub(r"[`*]", "", t.replace("\u2019", "'").lower())
    t = re.sub(r"(?<![a-z0-9$])_|_(?![a-z0-9])", "", t)          # _emphasis_
    t = re.sub(r"(?<![a-z0-9])-|-(?![a-z0-9])", " ", t)            # list dashes, not hyphens
    return re.sub(r"[^a-z0-9$_\\/.()'-]+", " ", t).replace(". ", " ").strip(" .")


def quote_source(url):
    m = re.match(rf"https://github\.com/{GH}/(?:issues|pull)/(\d+)", url or "")
    if m:
        i = gh_issue(int(m.group(1)))
        return (i.get("title") or "") + "\n\n" + (i.get("body") or "")
    m = re.match(rf"https://github\.com/{GH}/blob/{re.escape(P['base_branch'])}/([^#]+)", url or "")
    if m:
        return git("show", f"{MASTER}:{m.group(1)}")
    return None


QUOTE_DRIFT = []
for s_ in streams["streams"]:
    for t in s_.get("target", []):
        if t.get("summary"):
            t["verified"] = "summary"
            continue
        src = quote_source(t.get("url"))
        if src is None:
            t["verified"] = "no source"
            continue
        hay = _norm(src)
        parts = [x for x in re.split(r"\s*(?:\u2026|\.\.\.)\s*", t["text"]) if len(x.strip()) >= 10]
        t["verified"] = "verbatim" if parts and all(_norm(x) in hay for x in parts) else "not found"
        if t["verified"] == "not found":
            QUOTE_DRIFT.append({"area": s_["id"], "source": t.get("source"), "text": t["text"][:120]})
for g in streams.get("general", []):
    src = quote_source(g.get("url"))
    g["verified"] = "no source" if src is None else ("verbatim" if all(_norm(x) in _norm(src) for x in re.split(r"\s*(?:\u2026|\.\.\.)\s*", g["text"]) if len(x.strip()) >= 10) else "not found")
    if g["verified"] == "not found":
        QUOTE_DRIFT.append({"area": "general", "source": g.get("source"), "text": g["text"][:120]})
if QUOTE_DRIFT:
    (W / "quote-drift.json").write_text(json.dumps(QUOTE_DRIFT, indent=1))
    print(f"{len(QUOTE_DRIFT)} quote(s) no longer match their source; see {W / 'quote-drift.json'}", file=sys.stderr)
    if not os.environ.get("WIM_ALLOW_QUOTE_DRIFT"):
        sys.exit(2)

# --- per-release: dates, removed functions and classes, deleted files, measures ---------------------------
tag_info = {}
for t in TAGS:
    sha = git("rev-parse", t).strip()
    when = git("log", "-1", "--format=%cs", t).strip() if t != MASTER else str(date.today())
    tag_info[t] = {"name": P["base_branch"] if t == MASTER else re.sub(r"^v", "", t).replace("_", "."), "sha": sha[:10], "date": when}

cache_f = W / "measures.json"
cache = json.loads(cache_f.read_text()) if cache_f.exists() else {}
PHP_SCOPE = P["code_files"]


def measure(m, tagsha):
    key = f"{tagsha}|{m['kind']}|{m['pattern']}|{m.get('scope')}|{m.get('exclude')}"
    if key in cache:
        return cache[key]
    if m["kind"] == "regex":
        v = len(git("grep", "-hoE", m["pattern"], tagsha, "--", *PHP_SCOPE).splitlines())
    elif m["kind"] == "files":
        names = git("ls-tree", "-r", "--name-only", tagsha).splitlines()
        v = sum(1 for p in names if re.search(m["pattern"], p) and "vendor/" not in p and not p.startswith(("tests/", "node_modules/")))
    else:  # files_with: files in scope containing the pattern, minus excluded prefixes
        out = git("grep", "-lF", m["pattern"].replace("\\\\", "\\"), tagsha, "--", *m.get("scope", ["."]))
        files = [p.split(":", 1)[1] for p in out.splitlines()]
        v = sum(1 for p in files if not any(p.startswith(x) for x in m.get("exclude", [])) and "vendor/" not in p)
    cache[key] = v
    return v


RS = P.get("release_symbols") or {}  # where each release's global functions, classes and pages are listed (PHP)


def symbols_at(tagsha):
    fns = {re.sub(r"^function\s+&?([A-Za-z0-9_]+).*", r"\1", l) for l in
           git("grep", "-hE", r"^function\s+&?[A-Za-z_][A-Za-z0-9_]*\s*\(", tagsha, "--", *RS["functions"]).splitlines()} if RS.get("functions") else set()
    classes = {p.split(":", 1)[1] for p in git("grep", "-lE", r"^(final |abstract |readonly )*(class|interface|trait|enum) ", tagsha, "--",
                                                *RS["classes"]).splitlines()} if RS.get("classes") else set()
    return fns, classes


releases = []
prev = None
for t in TAGS:
    fns, classes = symbols_at(t)
    if prev:
        pt, pf, pc = prev
        deleted = [p for p in git("diff", "--diff-filter=D", "--name-only", pt, t, "--", ".", ":!vendor", ":!tests", ":!.phpstan").splitlines()]
        by_dir = Counter("/".join(p.split("/")[:3]) if p.count("/") >= 3 else "/".join(p.split("/")[:-1]) or "(root)" for p in deleted)
        pages = sorted(p for p in deleted if RS.get("pages_re") and re.match(RS["pages_re"], p) and not (RS.get("pages_skip") and RS["pages_skip"] in p))
        releases.append({"from": tag_info[pt]["name"], "to": tag_info[t]["name"], "date": tag_info[t]["date"],
                         "functions_removed": sorted(pf - fns), "functions_added": len(fns - pf),
                         "classes_removed": sorted(pc - classes), "classes_added": len(classes - pc),
                         "files_deleted": len(deleted), "deleted_by_dir": by_dir.most_common(12), "pages_deleted": pages[:40]})
    prev = (t, fns, classes)

# --- open PR records ----------------------------------------------------------------------------------------
pr_files = {n: sorted(e["files"]) for n, e in prs_git.items()}
refs = {}
for n, b in bodies.items():
    text = b.get("body") or ""
    fixes = {int(x) for x in re.findall(rf"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s*:?\s+(?:https://github\.com/{GH}/issues/|#)(\d+)", text)}
    mentions = {int(x) for x in re.findall(rf"(?:{GH}/(?:issues|pull)/|#)(\d{{3,5}})\b", text)}
    fixes |= set(extra.get(n, {}).get("closing", []))
    refs[n] = {"fixes": sorted(fixes - PLACEHOLDER_ISSUES), "mentions": sorted(mentions - PLACEHOLDER_ISSUES - {n})}

needs_rebase = set(findings["summary"]["conflict_with_master"])
master_conf = {int(k): v for k, v in findings.get("master_conflicts", {}).items()}

# --- streams ----------------------------------------------------------------------------------------------
out_streams = []
for s in streams["streams"]:
    plan = []
    for n in s["plan"]:
        i = gh_issue(n)
        if i and not (SKIP_LABELS & set(i.get("labels", []))):
            plan.append({"n": n, "title": i.get("title", ""), "state": i.get("state"), "by": i.get("user"), "updated": (i.get("updated_at") or "")[:10],
                         "pr": i.get("pr", False)})
    title_re = re.compile(s["title_re"]) if s.get("title_re") else None
    inflight = []
    for n, m in meta.items():
        if n in skip or m["author"]["login"] in ("app/dependabot", "dependabot"):
            continue
        why = []
        if set(s["plan"]) & set(refs.get(n, {}).get("mentions", []) + refs.get(n, {}).get("fixes", [])):
            why.append("cites the plan")
        if n in s["plan"]:
            why.append("in the plan")
        if set(s.get("labels", [])) & set(labels_of[n]):
            why.append("label")
        if s.get("paths") and any(f.startswith(tuple(s["paths"])) for f in pr_files.get(n, [])) and n not in mass:
            why.append("files")
        if title_re and title_re.search(m["title"]):
            why.append("title")
        if why:
            inflight.append({"n": n, "why": why})
    counts = []
    for msr in s.get("measures", []):
        if msr["kind"] == "checklist":
            # the maintainers' own record: ticked and open items in the tracker issue, as it reads today
            body = gh_issue(msr["issue"]).get("body") or ""
            done, todo = len(re.findall(r"(?im)^\s*- \[x\]", body)), len(re.findall(r"(?m)^\s*- \[ \]", body))
            counts.append({"label": msr["label"], "checklist": {"done": done, "total": done + todo}})
            continue
        counts.append({"label": msr["label"], "by_release": [{"tag": tag_info[t]["name"], "value": measure(msr, t)} for t in TAGS]})
    out_streams.append({**{k: s[k] for k in ("id", "title", "summary", "target", "before") if k in s},
                        **{k: s[k] for k in ("keys_leaving", "removals") if k in s},
                        "plan": plan, "inflight": sorted(inflight, key=lambda x: -x["n"]), "measures": counts})
cache_f.write_text(json.dumps(cache))

# --- same work twice ------------------------------------------------------------------------------------
same_issue = defaultdict(list)
for n, r in refs.items():
    if n in skip:
        continue
    for i in r["fixes"]:
        same_issue[i].append(n)
dups = []
for i, ns in sorted(same_issue.items()):
    if len(ns) > 1:
        iss = gh_issue(i)
        if SKIP_LABELS & set(iss.get("labels", [])):
            continue
        dups.append({"issue": i, "title": iss.get("title", ""), "state": iss.get("state"), "prs": sorted(ns),
                     "conflict": any(x["prs"] == sorted(ns)[:2] for x in findings["text_conflict"])})

# --- hot files: open PRs per file, mechanical files apart -------------------------------------------------
per_file = defaultdict(set)
for n, fs in pr_files.items():
    if n in mass or n in skip:
        continue
    for f in fs:
        per_file[f].add(n)
hot = sorted(((f, sorted(ns)) for f, ns in per_file.items() if len(ns) >= 4 and not MECHANICAL.match(f)), key=lambda x: -len(x[1]))[:30]
mech = sorted(((f, len(ns)) for f, ns in per_file.items() if MECHANICAL.match(f) and len(ns) >= 3), key=lambda x: -x[1])[:12]

# --- per-PR record for the lookup -------------------------------------------------------------------------
conf_with = defaultdict(list)
for x in findings["text_conflict"]:
    a, b = x["prs"]
    kind = "code" if x["code"] else "mechanical"
    conf_with[a].append({"n": b, "kind": kind, "files": x["code"] or x["mechanical"]})
    conf_with[b].append({"n": a, "kind": kind, "files": x["code"] or x["mechanical"]})
semantic = defaultdict(list)
for x in findings["signature"]:
    semantic[x["changes"]].append({"n": x["overrides"], "what": f"changes {x['method']}; PHP would refuse #{x['overrides']}'s override in {x['override_in'].split('/')[-1]}: {x['why']}"})
    semantic[x["overrides"]].append({"n": x["changes"], "what": f"its {x['override_in'].split('/')[-1]} overrides {x['method']}, which #{x['changes']} changes: {x['why']}"})
for x in findings["signature_vs_master"]:
    semantic[x["changes"]].append({"n": None, "what": f"changes {x['method']}; master's {x['master_file']} (added after this PR's base) overrides it: {x['why']}"})
for x in findings["removed_used"]:
    semantic[x["removes"]].append({"n": x["uses"], "what": f"removes {x['symbol']}, which #{x['uses']} calls"})
    semantic[x["uses"]].append({"n": x["removes"], "what": f"calls {x['symbol']}, which #{x['removes']} removes"})
for x in findings["removed_vs_master"]:
    semantic[x["removes"]].append({"n": None, "what": f"removes {x['symbol']}, which master now calls in {x['count']} file(s) the PR doesn't change, e.g. {x['master_callers'][0]}"})
for x in findings["deleted_changed"]:
    if x["mechanical"]:
        continue
    semantic[x["deletes"]].append({"n": x["changes"], "what": f"deletes {', '.join(x['files'])}, which #{x['changes']} changes"})
    semantic[x["changes"]].append({"n": x["deletes"], "what": f"changes {', '.join(x['files'])}, which #{x['deletes']} deletes"})
for x in findings["stacked"]:
    semantic[x["contained"]].append({"n": x["in"], "what": f"#{x['in']} is built on this PR; merging #{x['in']} merges both"})
    semantic[x["in"]].append({"n": x["contained"], "what": f"is built on #{x['contained']}; merging this merges both"})
for x in findings["same_method"]:
    a, b = x["prs"]
    semantic[a].append({"n": b, "what": f"both change {x['where'][0]} (merges cleanly as text)"})
    semantic[b].append({"n": a, "what": f"both change {x['where'][0]} (merges cleanly as text)"})
for x in findings["shared_name"]:
    for n in x["prs"]:
        semantic[n].append({"n": [m for m in x["prs"] if m != n], "what": f"adds the {x['kind']} {x['name']}, as do the others"})
stream_of = defaultdict(list)
for s in out_streams:
    for x in s["inflight"]:
        stream_of[x["n"]].append(s["id"])

vc = findings["version_claims"]
claim_of = {int(k): v for k, v in vc["claims"].items()}
shared_claim = {int(n): int(c) for c, ns in vc["shared"].items() for n in ns}
caps_alone = {int(k): v for k, v in findings["caps_alone"].items()}
caps_with = defaultdict(list)
for x in findings["caps_pairs"]:
    a, b = x["prs"]
    caps_with[a].append({"n": b, "caps": x["caps"]})
    caps_with[b].append({"n": a, "caps": x["caps"]})
same_issue_of = defaultdict(set)
for i, ns in same_issue.items():
    if len(ns) > 1:
        for n in ns:
            same_issue_of[n] |= set(ns) - {n}
BREAK = re.compile(r"^(changes |removes |deletes |calls |its |changes .*, which)")
today = date.today()

prs_out = {}
for n, m in meta.items():
    e = extra.get(n, {})
    latest = {}
    for r in e.get("reviews", []):
        if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"):
            latest[r["by"]] = r
    reviews = sorted(latest.values(), key=lambda r: r["at"])
    for r in reviews:
        r["bot"] = bool(BOT.search(r["by"]))
        r["maint"] = is_maint(r["by"], r["role"])
    maint = [r for r in reviews if r["maint"] and r["by"] != m["author"]["login"]]
    direction = prs_git.get(n, {}).get("direction", {})
    interacts = [] if n in skip else semantic.get(n, [])
    rec = {"t": m["title"], "by": m["author"]["login"], "role": e.get("author_role", ""), "draft": m["isDraft"],
           "by_maint": is_maint(m["author"]["login"], e.get("author_role", "")),
           "created": m["createdAt"][:10], "updated": m["updatedAt"][:10],
           "age": (today - date.fromisoformat(m["createdAt"][:10])).days,
           "files": len(prs_git.get(n, {}).get("files", {})), "add": e.get("additions"), "del": e.get("deletions"),
           "tests": sum(1 for f in prs_git.get(n, {}).get("files", {}) if f.startswith("tests/")),
           "mass": n in mass, "rebase": n in needs_rebase, "rebase_files": master_conf.get(n, [])[:8],
           "conflicts": conf_with.get(n, []), "interacts": interacts,
           "breaks": sum(1 for i in interacts if BREAK.match(i["what"])),
           "streams": stream_of.get(n, []), "fixes": refs.get(n, {}).get("fixes", []),
           "ci": e.get("ci"), "reviews": reviews, "maint": maint[-1]["state"] if maint else "",
           "maint_by": maint[-1]["by"] if maint else "",
           "dir": direction, "against": sorted(k for k, (a, r) in direction.items() if a > r),
           "toward": sorted(k for k, (a, r) in direction.items() if r > a),
           "claim": claim_of.get(n), "claim_shared": sorted(x for x, c in claim_of.items() if c == claim_of.get(n) and x != n) if n in shared_claim else [],
           "claim_taken": n in vc["taken"], "no_bump": n in vc["no_bump"],
           "caps_alone": caps_alone.get(n, []), "caps_with": caps_with.get(n, []),
           "same_issue": sorted(same_issue_of.get(n, [])),
           "head": prs_git.get(n, {}).get("head", ""),
           "stale_sql": findings.get("stale_upgrade", {}).get("prs", {}).get(str(n), [])}
    prs_out[n] = rec

# rebase share: how many PRs that need a rebase conflict only in mechanical files
rebase_mech = sum(1 for n in needs_rebase if master_conf.get(n) and all(MECHANICAL.match(f) for f in master_conf[n]))
# merge impact: PRs that would need a rebase if this one merged (code conflicts only)
impact = sorted(((n, sorted({c["n"] for c in cs if c["kind"] == "code"})) for n, cs in conf_with.items()), key=lambda x: -len(x[1]))
impact = [{"n": n, "code_conflicts": ns} for n, ns in impact if len(ns) >= 2][:20]

data = {
    "stamp": {"master": master_sha[:10], "master_full": master_sha, "generated": str(date.today()), "open_prs": len(meta), "drafts": sum(1 for m in meta.values() if m["isDraft"]),
              "mass": mass, "tags": [tag_info[t] for t in TAGS]},
    "general": streams["general"], "rules_package": streams["rules_package"],
    "streams": out_streams, "releases": releases,
    "duplicates": {"same_issue": dups, "shared_names": findings["shared_name"],
                   "stacked": findings["stacked"]},
    "merge": {"needs_rebase": len(needs_rebase), "needs_rebase_mechanical_only": rebase_mech,
              "pairs_sharing_a_file": findings["summary"]["pairs_sharing_a_file"],
              "conflict_pairs_code": sum(1 for x in findings["text_conflict"] if x["code"]),
              "conflict_pairs_mechanical": sum(1 for x in findings["text_conflict"] if not x["code"]),
              "mechanical_files": mech, "impact": impact,
              "baseline_deletion": [{"deletes": x["deletes"], "changed_by": x["changes"], "file": x["files"][0]} for x in findings["deleted_changed"] if x["mechanical"]],
              "version": {"master": vc["master"], "shared": vc["shared"], "taken": vc["taken"], "no_bump": vc["no_bump"]},
              "caps_alone": findings["caps_alone"], "caps_pairs": findings["caps_pairs"],
              "stale_upgrade": findings.get("stale_upgrade", {})},
    "direction_labels": {k: {"label": d["label"], "stream": d["stream"]} for k, d in DIRECTION.items()},
    "maintainer_roles": sorted(MAINTAINER_ROLES),
    "hot_files": hot,
    "files": {f: sorted(ns) for f, ns in per_file.items()},
    "mass_dirs": {n: sorted({"/".join(f.split("/")[:2]) for f in prs_git[n]["files"]}) for n in mass if n in prs_git},
    "prs": prs_out,
}
# --- the path forward: what is left per area, cut as each plan says, and its status ---------------------
sys.path.insert(0, str(HERE))
import todos  # noqa: E402
data["path"] = {
    "todos": todos.build(REPO, master_sha, streams, prs_out, per_file, data["mass_dirs"], refs, gh_issue, P),
    "rebase_mechanical": sorted(n for n in needs_rebase if master_conf.get(n) and all(MECHANICAL.match(f) for f in master_conf[n])),
}
data["maintainers"] = [{"login": k, "merged": v} for k, v in mergers.items() if k in MAINTAINERS]
data["profile"] = {k: P.get(k) for k in ("title", "project", "github", "base_branch", "mechanical_note", "left_out_note", "method_notes", "symbols")}
data["profile"]["version_noun"] = (P.get("version_claims") or {}).get("noun", "version number").strip("`")
data["profile"]["caps_noun"] = (P.get("exact_caps") or {}).get("noun", "lint cap")
data["profile"]["caps_file"] = (P.get("exact_caps") or {}).get("file")
data["profile"]["version_file"] = (P.get("version_claims") or {}).get("file")
data["profile"]["resource_label"] = P["anchors"].get("resource_label")
data["general_heading"] = streams.get("general_heading")
data["controls"] = json.loads((W / "controls-result.json").read_text()) if (W / "controls-result.json").exists() else []

# --- semantic readings (judgement): pairs read by agents from the packets, each checked by a second reading ---
SEM = G / "semantic"
items, tally = [], Counter()
if (SEM / "verdicts").is_dir():
    heads_then = json.loads((SEM / "heads.json").read_text())
    for f in sorted((SEM / "verdicts").glob("*.json")):
        v = json.loads(f.read_text())
        a_, b_ = (int(x) for x in v["pair"].split("-"))
        check = json.loads((SEM / "checks" / f.name).read_text()) if (SEM / "checks" / f.name).exists() else None
        then = heads_then["pairs"].get(v["pair"], {})
        stale = any(prs_git.get(x, {}).get("head") != then.get(str(x)) for x in (a_, b_))
        outcome = check["outcome"] if check else ("unchecked" if v["verdict"] in ("clash", "maybe") else "")
        tally[v["verdict"] if outcome != "refuted" else "refuted"] += 1
        if v["verdict"] in ("clash", "maybe") and outcome != "refuted":
            item = {**v, "a": a_, "b": b_, "outcome": outcome, "check_note": (check or {}).get("note", ""), "stale": stale,
                    "heads": then}
            items.append(item)
            for n_, m_ in ((a_, b_), (b_, a_)):
                if n_ in prs_out:
                    prs_out[n_].setdefault("judged", []).append({"n": m_, "verdict": v["verdict"], "kind": v.get("kind", ""), "summary": v["summary"],
                                                               "outcome": outcome, "stale": stale})
    data["semantic"] = {"candidates": heads_then.get("candidates"), "read": sum(tally.values()), "tally": dict(tally), "items": items,
                        "read_at": heads_then.get("read_at", "")[:10]}

(W / "map.json").write_text(json.dumps(data))
fill = {"%%TITLE%%": P["title"], "%%GITHUB%%": P["github"], "%%PROJECT%%": P["project"], "%%BASE%%": P["base_branch"],
        "%%MECH%%": P["mechanical_note"]}
page = TEMPLATE.read_text()
for k, v in fill.items():
    page = page.replace(k, html_escape(v))
page = page.replace("/*MAP_DATA*/null", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
OUT.write_text(page)
print(json.dumps({"streams": {s["id"]: {"plan": len(s["plan"]), "inflight": len(s["inflight"]), "measures": [m["by_release"][-1]["value"] if "by_release" in m else m["checklist"] for m in s["measures"]]} for s in out_streams},
                  "releases": [(r["to"], len(r["functions_removed"]), len(r["classes_removed"]), r["files_deleted"]) for r in releases],
                  "duplicates": len(dups), "hot_files": len(hot), "page_bytes": len(page)}, indent=1))

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
from datetime import date, datetime, timedelta
from itertools import combinations
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
TESTS_RE = re.compile(P["tests_re"]) if P.get("tests_re") else None  # a PR's test files; else those under tests/


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
    # a document outside the repository the profile's quote_sources name, as the fetch stage read it this build
    name = QUOTED.get(url or "")
    return (G / "quotes" / name).read_text() if name else None


QUOTED = json.loads((G / "quotes.json").read_text()) if (G / "quotes.json").exists() else {}


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
# a PR aimed at a release branch needs a rebase when it conflicts with that branch; a conflict further up the chain is
# for whoever merges up to resolve, and is shown on its card instead
merge_up = {int(k): v for k, v in findings.get("merge_up", {}).items()}
for n, m in merge_up.items():
    own = m["path"][0]["conflicts"]
    needs_rebase.discard(n)
    master_conf.pop(n, None)
    if own:
        needs_rebase.add(n)
        master_conf[n] = own

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
    # an area that quotes no target or gives no advice before building has empty lists, not none: the page reads both
    out_streams.append({"target": [], "before": [], **{k: s[k] for k in ("id", "title", "summary", "target", "before") if k in s},
                        **{k: s[k] for k in ("keys_leaving", "keys_heading", "removals") if k in s},
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
# With "stale_conflicts": "apart", a code conflict found branch to branch (one of the two needs a rebase onto the base
# first) is its own kind: shown on both cards, but neither joining a group nor counting against the PR that merges.
# On php-src it joined 312 PRs into one group through PRs that conflict with their base; left apart, the largest is 28.
STALE_APART = P.get("stale_conflicts") == "apart"
for x in findings["text_conflict"]:
    a, b = x["prs"]
    kind = ("after a rebase" if STALE_APART and x["how"] == "branch to branch" else "code") if x["code"] else "mechanical"
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

# --- conformance: the outside standards each PR touches; its controls stop the build when they fail -------
sys.path.insert(0, str(HERE))
import conformance  # noqa: E402
CONF = P.get("conformance") or {"specs": []}
conf_prs = {n: {"files": pr_files.get(n, []), "title": m["title"]} for n, m in meta.items() if n not in skip}
conf_workflows = {f: git("show", f"{MASTER}:{f}") for f in git("ls-tree", "-r", "--name-only", MASTER, "--", ".github/workflows/").splitlines()}
# the names checks search the code for every listed name and for each control's planted one, so a matcher that
# matched anything would find the planted name and fail its control
conf_names = sorted({n for s in CONF.get("specs", []) for n in (s.get("names") or {}).get("names", [])}
                    | {c["planted"] for c in CONF.get("controls", []) if "planted" in c})
conf_where = sorted({w for s in CONF.get("specs", []) for w in (s.get("names") or {}).get("where", [])})
conf_text = git("grep", "-h", "-o", "-E", "[\"'/](" + "|".join(re.escape(n) for n in conf_names) + ")[\"']", MASTER, "--", *conf_where) if conf_names else ""
conf_fail, conf_notes = conformance.check(CONF, git("ls-tree", "-r", "--name-only", MASTER).splitlines(), conf_prs, conf_workflows, conf_text)
for note in conf_notes:
    print(f"conformance: {note}", file=sys.stderr)
if conf_fail:
    (W / "conformance-controls.json").write_text(json.dumps(conf_fail, indent=1))
    print(f"{len(conf_fail)} conformance control(s) failed; see {W / 'conformance-controls.json'}", file=sys.stderr)
    sys.exit(2)
conf_reported = json.loads((G / "conformance-issues.json").read_text()) if (G / "conformance-issues.json").exists() else {}
conf_specs, conf_of = conformance.build(CONF, conf_prs, set(mass), conf_workflows, conf_text, conf_reported, gh_issue)

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


# --- owners: whom GitHub asks to review each PR, by the CODEOWNERS file on the PR's own target branch (php-src's
# PHP-8.2 has 64 rules, master 69). A pattern works as in .gitignore: a leading / anchors it at the root, a pattern
# with no / matches at any depth, and a directory covers what is under it. For each file the last matching rule wins.
def codeowners_regex(pattern):
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    p, rx, i = pattern.strip("/"), "", 0
    while i < len(p):
        if p.startswith("**/", i):
            rx, i = rx + "(?:.*/)?", i + 3
        elif p.startswith("**", i):
            rx, i = rx + ".*", i + 2
        elif p[i] == "*":
            rx, i = rx + "[^/]*", i + 1
        elif p[i] == "?":
            rx, i = rx + "[^/]", i + 1
        else:
            rx, i = rx + re.escape(p[i]), i + 1
    return re.compile(("^" if anchored else "^(?:.*/)?") + rx + "(?:/.*)?$")


def codeowners_rules(rev):
    """[(regex, owners)] from the first CODEOWNERS GitHub reads at rev, in file order."""
    for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        text = git("show", f"{rev}:{path}")
        if text:
            rules = []
            for line in text.splitlines():
                parts = line.split("#", 1)[0].split()
                if len(parts) > 1:
                    rules.append((codeowners_regex(parts[0]), [o for o in parts[1:] if o.startswith("@") or "@" in o]))
            return rules
    return []


def owners_of(files, rules):
    out = set()
    for f in files:
        hit = None
        for rx, owners in rules:
            if rx.match(f):
                hit = owners
        out |= set(hit or [])
    return sorted(out)


RULES = {}
if P.get("codeowners", True):
    for b in {m_.get("baseRefName") or P["base_branch"] for m_ in meta.values()}:
        RULES[b] = codeowners_rules(MASTER if b == P["base_branch"] else f"{P['remote']}/{b}")
owners = {n: owners_of(prs_git.get(n, {}).get("files", {}), RULES.get(m_.get("baseRefName") or P["base_branch"], [])) for n, m_ in meta.items()}

# --- one fix opened twice: the same title and a file in common outside the mechanical ones. php-src opens a fix on a
# release branch and again on master under one title (#24102 on PHP-8.6, #24103 on master).
by_title = defaultdict(list)
for n, m_ in meta.items():
    if n not in skip:
        by_title[" ".join(m_["title"].lower().split())].append(n)
same_title_of, twins = defaultdict(set), []
for t, ns in by_title.items():
    for a, b in combinations(sorted(ns), 2):
        shared = sorted((set(pr_files.get(a, [])) & set(pr_files.get(b, []))) - {f for f in pr_files.get(a, []) if MECHANICAL.match(f)})
        if shared:
            same_title_of[a].add(b)
            same_title_of[b].add(a)
            twins.append({"title": meta[a]["title"].strip(), "prs": [a, b], "bases": [meta[a].get("baseRefName"), meta[b].get("baseRefName")], "files": shared[:3]})

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
           "tests": sum(1 for f in prs_git.get(n, {}).get("files", {}) if (TESTS_RE.search(f) if TESTS_RE else f.startswith("tests/"))),
           "base": m.get("baseRefName") or P["base_branch"],
           "merge_up": [{"branch": x["branch"], "conflicts": x["conflicts"][:8]} for x in merge_up[n]["path"][1:] if x["conflicts"]] if n in merge_up else [],
           "mass": n in mass, "rebase": n in needs_rebase, "rebase_files": master_conf.get(n, [])[:8],
           "conflicts": conf_with.get(n, []), "interacts": interacts,
           "breaks": sum(1 for i in interacts if BREAK.match(i["what"])),
           "streams": stream_of.get(n, []), "conf": conf_of.get(n, []), "fixes": refs.get(n, {}).get("fixes", []),
           "ci": e.get("ci"), "ci_failing": e.get("ci_failing") or [], "reviews": reviews, "maint": maint[-1]["state"] if maint else "",
           "maint_by": maint[-1]["by"] if maint else "",
           "dir": direction, "against": sorted(k for k, (a, r) in direction.items() if a > r),
           "toward": sorted(k for k, (a, r) in direction.items() if r > a),
           "claim": claim_of.get(n), "claim_shared": sorted(x for x, c in claim_of.items() if c == claim_of.get(n) and x != n) if n in shared_claim else [],
           "claim_taken": n in vc["taken"], "no_bump": n in vc["no_bump"],
           "caps_alone": caps_alone.get(n, []), "caps_with": caps_with.get(n, []),
           "same_issue": sorted(same_issue_of.get(n, [])),
           "same_title": sorted(same_title_of.get(n, [])),
           "owners": owners.get(n, []),
           "labels": labels_of.get(n, []),
           "head": prs_git.get(n, {}).get("head", ""),
           "stale_sql": findings.get("stale_upgrade", {}).get("prs", {}).get(str(n), []),
           "stale_only": str(n) in map(str, findings.get("stale_upgrade", {}).get("only", []))}
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
    "conformance": {**{k: v for k, v in CONF.items() if k not in ("specs", "controls", "_about", "issues")}, "specs": conf_specs,
                    "reported": conf_reported.get("_general"),
                    "controls_passed": len(CONF.get("controls", [])) - len(conf_notes)} if CONF.get("specs") else None,
    "duplicates": {"same_issue": dups, "same_title": twins, "shared_names": findings["shared_name"],
                   "stacked": findings["stacked"]},
    "merge": {"needs_rebase": len(needs_rebase), "needs_rebase_mechanical_only": rebase_mech,
              "pairs_sharing_a_file": findings["summary"]["pairs_sharing_a_file"],
              "conflict_pairs_code": sum(1 for x in findings["text_conflict"] if x["code"] and not (STALE_APART and x["how"] == "branch to branch")),
              "conflict_pairs_after_rebase": sum(1 for x in findings["text_conflict"] if x["code"] and STALE_APART and x["how"] == "branch to branch"),
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
data["profile"] = {k: P.get(k) for k in ("title", "project", "github", "base_branch", "mechanical_note", "left_out_note", "method_notes", "symbols",
                                          "mass_files", "skip_labels", "tool_version", "preset", "ci_ignore")}
data["profile"]["version_noun"] = (P.get("version_claims") or {}).get("noun", "version number").strip("`")
data["profile"]["caps_noun"] = (P.get("exact_caps") or {}).get("noun", "lint cap")
data["profile"]["caps_file"] = (P.get("exact_caps") or {}).get("file")
data["profile"]["version_file"] = (P.get("version_claims") or {}).get("file")
data["profile"]["resource_label"] = P["anchors"].get("resource_label")
data["profile"]["merge_up"] = P.get("merge_up") or []
data["profile"]["pair_files"] = P.get("pair_files") or "all"
data["profile"]["landings"] = P.get("landings") or "merge"
data["merge_up"] = findings["summary"].get("merge_up")
data["general_heading"] = streams.get("general_heading")
data["controls"] = json.loads((W / "controls-result.json").read_text()) if (W / "controls-result.json").exists() else []
# open PRs aimed at another branch (a release branch's backports): named, not compared, since they merge elsewhere
data["other_bases"] = json.loads((G / "other-bases.json").read_text()) if (G / "other-bases.json").exists() else []

# --- semantic readings (judgement): pairs read by agents from the packets, each checked by a second reading ---
SEM = G / "semantic"
items, tally = [], Counter()
if (SEM / "verdicts").is_dir():
    # each pair's heads at reading time (written by semantic_packets.py); without it, no reading can show as stale
    heads_then = json.loads((SEM / "heads.json").read_text()) if (SEM / "heads.json").exists() else {"pairs": {}, "candidates": None, "read_at": ""}
    for f in sorted((SEM / "verdicts").glob("*.json")):
        v = json.loads(f.read_text())
        a_, b_ = (int(x) for x in v["pair"].split("-"))
        check = json.loads((SEM / "checks" / f.name).read_text()) if (SEM / "checks" / f.name).exists() else None
        then = heads_then["pairs"].get(v["pair"], {})
        stale = bool(then) and any(prs_git.get(x, {}).get("head") != then.get(str(x)) for x in (a_, b_))
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

# --- groups: open PRs joined by a link that needs a decision about order: a code conflict, a break, a stack, a shared
# name, the same issue, or a clash a reading confirmed. Mechanical conflicts are left out: every schema or lint change
# shares those files, and they would join much of the queue into one group whose only decision is a rebase. A PR over
# the file limit was never compared pair by pair, so it is counted apart and never as standing alone.
links = defaultdict(set)


def link(a, b, kind):
    if a is None or b is None:  # a break against the base itself names no partner PR
        return
    a, b = int(a), int(b)
    if a != b and a in prs_out and b in prs_out:
        links[(min(a, b), max(a, b))].add(kind)


for n, p in prs_out.items():
    for c in p["conflicts"]:
        if c["kind"] == "code":
            link(n, c["n"], "code conflict")
    for i in p["interacts"]:
        if BREAK.match(i["what"]):
            for t in (i["n"] if isinstance(i["n"], list) else [i["n"]]):
                link(n, t, "break")
    for m_ in p["same_issue"]:
        link(n, m_, "same issue")
    for m_ in p["same_title"]:
        link(n, m_, "same title")
for x in findings["stacked"]:
    link(x["in"], x["contained"], "stacked")
for x in findings["shared_name"]:
    for i, a in enumerate(x["prs"]):
        for b in x["prs"][i + 1:]:
            link(a, b, "same name")
for x in (data.get("semantic") or {}).get("items", []):
    if x["verdict"] == "clash":
        link(x["a"], x["b"], "clash")
parent = {}


def root(x):
    while parent.setdefault(x, x) != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


for a, b in links:
    parent[root(a)] = root(b)
members = defaultdict(list)
for x in parent:
    members[root(x)].append(x)
groups = sorted((sorted(ms) for ms in members.values()), key=lambda ms: (-len(ms), ms[0]))
data["groups"] = [{"prs": ms, "links": [{"a": a, "b": b, "kinds": sorted(links[(a, b)])} for (a, b) in sorted(links) if a in ms]}
                  for ms in groups]
for gi, g in enumerate(data["groups"], 1):
    for n in g["prs"]:
        prs_out[n]["group"] = gi
data["glance"] = {"open": len(prs_out), "mass": len(mass), "groups": len(groups), "in_groups": sum(len(g) for g in groups),
                  "alone": sum(1 for n, p in prs_out.items() if not p.get("group") and n not in mass),
                  "clashes": sum(1 for x in (data.get("semantic") or {}).get("items", []) if x["verdict"] == "clash")}

# --- since the last build: each build keeps a snapshot of every PR's standing, and the page compares this one with
# the newest snapshot at least WIM_SINCE_HOURS old (default 12), so rebuilding twice in a day still reads as "since
# yesterday". "Nothing blocking" is decided here as on the page: the browser check holds the two to the same answer.
STACKED = {(x["in"], x["contained"]) for x in findings["stacked"]} | {(x["contained"], x["in"]) for x in findings["stacked"]}


def blockers(n, p):
    """Why a PR is not in "Nothing blocking"; empty when it is."""
    out = []
    if p["draft"]:
        out.append("draft")
    if p["mass"]:
        out.append("too large to compare")
    elif p["rebase"]:
        out.append("needs a rebase")
    if p["ci"] != "SUCCESS":
        out.append({"FAILURE": "CI failing", "ERROR": "CI error", "PENDING": "CI pending", "EXPECTED": "CI waiting"}.get(p["ci"], "CI not run"))
    if p["maint"] == "CHANGES_REQUESTED":
        out.append("changes requested")
    if any(c["kind"] == "code" for c in p["conflicts"]):
        out.append("code conflict")
    if p["breaks"]:
        out.append("breaks another PR")
    if p["against"]:
        out.append("adds what the plan retires")
    if p["claim_taken"] or p["no_bump"] or any((n, m) not in STACKED for m in p["claim_shared"]):
        out.append("version number")
    if p["caps_alone"] or p["caps_with"]:
        out.append("caps")
    if p["stale_only"]:
        out.append("upgrade SQL in an earlier release's file")
    return out


for n, p in prs_out.items():
    p["shortlist"] = not blockers(n, p)

# --- status labels the build disagrees with: the profile's status_labels name what a label says a PR waits on.
# "review": labelled ready for reviewers, while something the author must fix first stands in the way; "author":
# labelled waiting on its author, while nothing these checks can see blocks it. On php-src all 15 PRs labelled
# "Waiting on Review" were 3 to 7 years old, none with CI on its head and 12 needing a rebase.
STATUS = P.get("status_labels") or {}
label_gaps = []
for n, p in sorted(prs_out.items(), key=lambda x: -x[1]["age"]):
    for label, waits_on in STATUS.items():
        if label not in p["labels"]:
            continue
        why = blockers(n, p)
        if waits_on == "review" and why:
            label_gaps.append({"n": n, "label": label, "says": "ready for review", "but": why})
        elif waits_on == "author" and not why:
            label_gaps.append({"n": n, "label": label, "says": "waiting on its author", "but": ["nothing these checks can see blocks it"]})
code_pairs = {tuple(sorted((n, c["n"]))) for n, p in prs_out.items() for c in p["conflicts"] if c["kind"] == "code"}
snapshot = {"at": datetime.now().isoformat(timespec="seconds"), "master": master_sha,
            "prs": {n: {"t": p["t"], "by": p["by"], "draft": p["draft"], "ci": p["ci"], "rebase": p["rebase"], "maint": p["maint"],
                        "maint_by": p["maint_by"], "shortlist": p["shortlist"], "head": p["head"]} for n, p in prs_out.items()},
            "code_pairs": sorted(code_pairs)}
HIST = G / "history"
HIST.mkdir(exist_ok=True)
older = sorted(HIST.glob("*.json"))
cutoff = datetime.now() - timedelta(hours=float(os.environ.get("WIM_SINCE_HOURS", "12")))
# the newest snapshot old enough; before a day of history exists, the oldest one (the page states its time)
base_f = next((f for f in reversed(older) if datetime.fromisoformat(json.loads(f.read_text())["at"]) <= cutoff), older[0] if older else None)
data["since"] = None


def closed_by_commit(n):
    """The committer of the commit that closed PR n, or None when no commit closed it (php-src lands most PRs this way)."""
    q = ('query($n: Int!) { repository(owner: "%s", name: "%s") { pullRequest(number: $n) { timelineItems(itemTypes: [CLOSED_EVENT], last: 1) '
         '{ nodes { ... on ClosedEvent { closer { __typename ... on Commit { committer { user { login } } } } } } } } } }') % (P["owner"], P["name"])
    r = subprocess.run(["gh", "api", "graphql", "-f", f"query={q}", "-F", f"n={n}"], capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        return None
    nodes = json.loads(r.stdout)["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"]
    closer = (nodes[-1] if nodes else {}).get("closer") or {}
    return ((closer.get("committer") or {}).get("user") or {}).get("login") or "" if closer.get("__typename") == "Commit" else None


if base_f:
    before = json.loads(base_f.read_text())
    old = {int(k): v for k, v in before["prs"].items()}
    # a PR now aimed at another branch left the list but is still open: neither merged nor closed
    elsewhere = {o["number"]: o["base"] for o in data["other_bases"]}
    gone = sorted(set(old) - set(prs_out) - set(elsewhere))
    fates = {}
    for n in gone[:40]:
        if os.environ.get("WIM_OFFLINE"):
            break
        r = subprocess.run(["gh", "api", f"repos/{P['github']}/pulls/{n}", "--jq", "{state: .state, merged: .merged, by: (.merged_by.login // \"\")}"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip() and json.loads(r.stdout)["state"] == "closed":
            fates[n] = json.loads(r.stdout)
            if not fates[n]["merged"] and P.get("landings") == "merge+commit":
                committer = closed_by_commit(n)
                if committer is not None:  # landed by pushing its commits: merged, by the one who pushed them
                    fates[n].update(merged=True, by=committer)
    both = set(old) & set(prs_out)
    old_pairs = {tuple(x) for x in before.get("code_pairs", [])}
    data["since"] = {
        "at": before["at"], "master": before["master"][:10],
        "merged": [{"n": n, "t": old[n]["t"], "by": old[n]["by"], "merged_by": fates[n]["by"]} for n in gone if fates.get(n, {}).get("merged")],
        "closed": [{"n": n, "t": old[n]["t"], "by": old[n]["by"]} for n in gone if n in fates and not fates[n]["merged"]],
        "gone": [{"n": n, "t": old[n]["t"], "by": old[n]["by"]} for n in gone if n not in fates],
        "elsewhere": [{"n": n, "t": old[n]["t"], "by": old[n]["by"], "base": elsewhere[n]} for n in sorted(set(old) & set(elsewhere))],
        "new": sorted(n for n in prs_out if n not in old),
        "entered": sorted(n for n in prs_out if prs_out[n]["shortlist"] and not old.get(n, {}).get("shortlist")),
        "left": [{"n": n, "why": blockers(n, prs_out[n])} for n in sorted(both) if old[n]["shortlist"] and not prs_out[n]["shortlist"]],
        "rebase": sorted(n for n in both if prs_out[n]["rebase"] and not old[n]["rebase"]),
        "conflicts": [list(x) for x in sorted(code_pairs - old_pairs) if x[0] in old and x[1] in old],
        "reviews": [{"n": n, "state": prs_out[n]["maint"], "by": prs_out[n]["maint_by"]} for n in sorted(both)
                    if prs_out[n]["maint"] in ("APPROVED", "CHANGES_REQUESTED") and prs_out[n]["maint"] != old[n]["maint"]],
    }
(HIST / f"{datetime.now():%Y%m%dT%H%M%S}.json").write_text(json.dumps(snapshot))
for f in sorted(HIST.glob("*.json"))[:-30]:  # keep the newest 30
    f.unlink()

data["labels_vs_build"] = {"labels": STATUS, "gaps": label_gaps}

# --- the same records as data: one row per open PR (CSV for a spreadsheet, JSON for tools) and the groups, written
# next to the page, which links them
GH_URL = f"https://github.com/{P['github']}/pull/"
records = []
for n, p in sorted(prs_out.items()):
    records.append({
        "number": n, "url": GH_URL + str(n), "title": p["t"].strip(), "author": p["by"], "base": p["base"], "draft": p["draft"],
        "created": p["created"], "updated": p["updated"], "age_days": p["age"], "files": p["files"], "additions": p["add"], "deletions": p["del"],
        "tests_changed": p["tests"], "ci": p["ci"], "ci_failing": p["ci_failing"], "maintainer_review": p["maint"] or None, "maintainer_reviewer": p["maint_by"] or None,
        "nothing_blocking": p["shortlist"], "blockers": blockers(n, p), "needs_rebase": p["rebase"], "rebase_files": p["rebase_files"],
        "merge_up_conflicts": p["merge_up"], "over_file_limit": p["mass"],
        "code_conflicts": sorted(c["n"] for c in p["conflicts"] if c["kind"] == "code"),
        "conflicts_after_a_rebase": sorted(c["n"] for c in p["conflicts"] if c["kind"] == "after a rebase"),
        "mechanical_conflicts": sorted(c["n"] for c in p["conflicts"] if c["kind"] == "mechanical"),
        "breaks": p["breaks"], "group": p.get("group"), "same_issue": p["same_issue"], "same_title": p["same_title"],
        "areas": p["streams"], "owners": p["owners"], "labels": p["labels"], "fixes": p["fixes"],
        "changed_files": pr_files.get(n, []),
    })
stamp = {"repository": P["github"], "base_branch": P["base_branch"], "base_commit": master_sha, "generated": str(date.today()),
         "tool": f"pr-toolbox motion {P.get('tool_version') or ''}".strip()}
DATA = {"prs_json": f"{OUT.stem}-prs.json", "prs_csv": f"{OUT.stem}-prs.csv", "groups_json": f"{OUT.stem}-groups.json"}
(OUT.parent / DATA["prs_json"]).write_text(json.dumps({"schema": "pr-toolbox motion prs 1", **stamp, "prs": records}, indent=1))
(OUT.parent / DATA["groups_json"]).write_text(json.dumps({"schema": "pr-toolbox motion groups 1", **stamp, "groups": data["groups"]}, indent=1))
import csv
import io
buf = io.StringIO()
wr = csv.writer(buf)
cols = [c for c in (records[0] if records else {}) if c != "changed_files"]  # file lists stay in the JSON
wr.writerow(cols)
for r in records:
    wr.writerow(["; ".join(str(x) if not isinstance(x, dict) else f"{x['branch']}: {' '.join(x['conflicts'])}" for x in v) if isinstance(v, list)
                 else "" if v is None else v for v in (r[c] for c in cols)])
(OUT.parent / DATA["prs_csv"]).write_text(buf.getvalue())
data["exports"] = DATA

(W / "map.json").write_text(json.dumps(data))
fill = {"%%TITLE%%": P["title"], "%%GITHUB%%": P["github"], "%%PROJECT%%": P["project"], "%%BASE%%": P["base_branch"],
        "%%ONTO%%": "their own branch" if P.get("merge_up") else P["base_branch"],
        "%%MECH%%": P["mechanical_note"]}
page = TEMPLATE.read_text()
for k, v in fill.items():
    page = page.replace(k, html_escape(v))
page = page.replace("/*MAP_DATA*/null", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
OUT.write_text(page)
print(json.dumps({"streams": {s["id"]: {"plan": len(s["plan"]), "inflight": len(s["inflight"]), "measures": [m["by_release"][-1]["value"] if "by_release" in m else m["checklist"] for m in s["measures"]]} for s in out_streams},
                  "releases": [(r["to"], len(r["functions_removed"]), len(r["classes_removed"]), r["files_deleted"]) for r in releases],
                  "duplicates": len(dups), "hot_files": len(hot), "page_bytes": len(page)}, indent=1))

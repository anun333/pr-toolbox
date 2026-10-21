# SPDX-License-Identifier: GPL-3.0-or-later
"""Pairs of open PRs worth reading for a semantic clash: two PRs from different authors that meet on the same
concept without needing to share a file, where git and the checks in repo_audit.py see nothing. Anchors, each
switched on by the profile's "anchors":
  - resource        both change the same domain resource's services, controllers or routes (OpenEMR: FHIR);
  - table           both write the same table, or one writes it and the other reads it;
  - route family    both add routes under the same family;
  - behaviour       one changes a method and the other adds calls to it.
Paths, classes and methods matching the profile's "left_out_re", PRs with its "skip_labels", and PRs over the mass
limit are left out.
Reads the audit's caches and git objects as text; runs nothing from any PR.

    WIM_PROFILE=<profile.json> python3 -I semantic_candidates.py <clone> <cache dir> [how many to read] > candidates.json
"""
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

REPO, C = Path(sys.argv[1]), Path(sys.argv[2])
W = C / "audit"
P = json.loads(Path(os.environ["WIM_PROFILE"]).read_text())
A = P["anchors"]
MASTER = os.environ.get("WIM_MASTER") or f"{P['remote']}/{P['base_branch']}"
SKIP_LABELS = set(P["skip_labels"])
ACL = re.compile(P["left_out_re"])
# helpers every PR calls say nothing about a shared concept
INFRA = re.compile(A["infra_re"])
TEST = re.compile(A["test_re"])
COMMON_METHODS = set(A["common_methods"])
RESOURCE = A.get("resource_label") or "resource"


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).stdout


prs = {int(k): v for k, v in json.loads((W / "prs.json").read_text()).items()}
symbols = json.loads((W / "symbols.json").read_text()) if (W / "symbols.json").exists() else {}
findings = json.loads((W / "findings.json").read_text())
meta = {p["number"]: p for p in json.loads((C / "pr-meta.json").read_text())}
mass = {int(n) for n in findings["summary"]["mass"]}
skip = {n for n, m in meta.items() if SKIP_LABELS & {l["name"] for l in m.get("labels", [])}}
author = {n: m["author"]["login"] for n, m in meta.items()}
conflicting = {tuple(x["prs"]) for x in findings["text_conflict"]}
stacked = {tuple(sorted((x["contained"], x["in"]))) for x in findings["stacked"]}

# the domain's resource names, from a folder of one file per resource (OpenEMR: the generated FHIR R4 classes)
RESOURCES = sorted({Path(p).stem[len(A.get("resource_prefix") or ""):] for p in git("ls-tree", "--name-only", MASTER, A["resources_dir"]).split()
                    if p.endswith(A.get("resource_ext") or "")} if A.get("resources_dir") else set(), key=len, reverse=True)
TABLES = set(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)?\s+`?(\w+)`?", git("show", f"{MASTER}:{A['schema_file']}"), re.I)) \
    if A.get("schema_file") else set()

WRITE = re.compile(r"(?i)\b(?:INSERT\s+(?:IGNORE\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+`?(\w+)`?")
READ = re.compile(r"(?i)\b(?:FROM|JOIN)\s+`?(\w+)`?")
ROUTE = re.compile(A["route_re"]) if A.get("route_re") else None
ROUTE_FILES = A.get("route_prefix") or "\0"
CALL = re.compile(r"(?:->|::)(\w+)\s*\(")


def resource_of(path):
    """The domain resource a file belongs to: by its folder, or by a file name that starts with the resource."""
    if not RESOURCES:
        return None
    m = re.match(A["resource_folder_re"], path) if A.get("resource_folder_re") else None
    if m and m.group(1) in RESOURCES:
        return m.group(1)
    base, prefix = path.rsplit("/", 1)[-1], A.get("resource_file_prefix") or ""
    if base.startswith(prefix) and A.get("resource_file_re") and re.search(A["resource_file_re"], base):
        return next((r for r in RESOURCES if base[len(prefix):].startswith(r)), None)
    return None


def changed_methods(entry):
    """Class::method units whose old lines a PR changes (not tests, not constructors)."""
    out = set()
    for path, info in entry["php"].items():
        if TEST.match(path) or not info.get("old_lines") or not info.get("base_blob"):
            continue
        s = symbols.get(f"{info['base_blob']}.php", {})
        for c in s.get("classes", []):
            for m in c["methods"]:
                if m["name"] == "__construct":
                    continue
                if any(m["start"] <= start + max(count, 1) - 1 and m["end"] >= start for start, count in info["old_lines"]):
                    out.add((c["name"], m["name"], path))
    return out


anchors = defaultdict(lambda: defaultdict(set))  # (kind, name) -> pr -> {role}
calls = {}
for n, e in prs.items():
    if n in mass or n in skip or n not in meta:
        continue
    for path in e["files"]:
        if ACL.search(path) or TEST.match(path):
            continue
        r = resource_of(path)
        if r:
            anchors[(RESOURCE, r)][n].add("changes")
    texts = {**e.get("added_by_file", {}), **e.get("name_lines", {})}
    for path, text in texts.items():
        if ACL.search(path) or TEST.match(path):
            continue
        for t in WRITE.findall(text):
            if t in TABLES and not ACL.search(t):
                anchors[("table", t)][n].add("writes")
        for t in READ.findall(text):
            if t in TABLES and not ACL.search(t):
                anchors[("table", t)][n].add("reads")
        if ROUTE and path.startswith(ROUTE_FILES):
            for kind, fam in ROUTE.findall(text):
                anchors[("route family", f"/{kind}/{fam}")][n].add("adds routes")
                if kind == A.get("resource_route_kind") and fam in RESOURCES:
                    anchors[(RESOURCE, fam)][n].add("adds routes")
    calls[n] = {path: set(CALL.findall(text)) for path, text in e.get("added_by_file", {}).items() if not TEST.match(path)}
    for cls, meth, path in changed_methods(e):
        if not ACL.search(path) and not ACL.search(cls) and not ACL.search(meth):
            anchors[("method", f"{cls}::{meth}")][n].add("changes")

pairs = defaultdict(list)
WEIGHT = {RESOURCE: 3, "route family": 2, "behaviour": 3}
rare = lambda df: min(1.0, 3 / df)  # an anchor three PRs share says more than one forty share
for (kind, name), by_pr in anchors.items():
    ns = sorted(by_pr)
    if kind == "method":
        continue
    for a, b in combinations(ns, 2):
        if author.get(a) == author.get(b):
            continue
        ra, rb = by_pr[a], by_pr[b]
        if kind == "table":
            if "writes" not in ra | rb:
                continue
            both_write = "writes" in ra and "writes" in rb
            if len(ns) > 15 and not both_write:  # a table everything reads: only writers against writers
                continue
            w = (3 if both_write else 2) * rare(len(ns))
        else:
            w = WEIGHT[kind] * rare(len(ns))
        pairs[(a, b)].append({"kind": kind, "name": name, "a": sorted(ra), "b": sorted(rb), "weight": round(w, 2), "shared_by": len(ns)})

# behaviour: A changes Class::method, B newly calls that method in a file that names the class
for (kind, name), by_pr in anchors.items():
    if kind != "method":
        continue
    cls, meth = name.split("::")
    if meth in COMMON_METHODS or len(meth) < 6 or INFRA.search(name):
        continue
    callers = [b for b, files in calls.items() if any(meth in c for c in files.values())]
    for a in by_pr:
        for b, files in calls.items():
            if b == a or author.get(a) == author.get(b) or b in mass or b in skip:
                continue
            hit = [p for p, called in files.items() if meth in called]
            if not hit:
                continue
            named = [p for p in hit if re.search(rf"\b{re.escape(cls)}\b", git("show", f"refs/pr/{b}:{p}"))]
            if named:
                key = tuple(sorted((a, b)))
                pairs[key].append({"kind": "behaviour", "name": name, "changes": a, "calls": b, "in": named[:3],
                                   "weight": round(3 * rare(len(callers)), 2), "shared_by": len(callers)})

out = []
for (a, b), anc in pairs.items():
    score = sum(x["weight"] for x in anc)
    tc = (a, b) in conflicting
    score = round(score + (0 if tc else 0.5) + (0.5 if not meta[a]["isDraft"] and not meta[b]["isDraft"] else 0), 2)
    out.append({"a": a, "b": b, "score": score, "anchors": anc, "text_conflict": tc, "stacked": (a, b) in stacked,
                "titles": [meta[a]["title"], meta[b]["title"]], "authors": [author[a], author[b]]})
out.sort(key=lambda x: (-x["score"], x["a"], x["b"]))
# what gets read: best first, each PR in at most PER_PR pairs, so two broad PRs can't fill the list
PER_PR, READ_N = 4, int(sys.argv[3]) if len(sys.argv) > 3 else 40
seen, chosen = defaultdict(int), []
for x in out:
    if len(chosen) >= READ_N:
        break
    if seen[x["a"]] < PER_PR and seen[x["b"]] < PER_PR:
        chosen.append(x)
        seen[x["a"]] += 1
        seen[x["b"]] += 1
for i, x in enumerate(out):
    x["read"] = x in chosen
control = next((x for x in out if {x["a"], x["b"]} == {10926, 14708}), None)
print(json.dumps({"master": git("rev-parse", MASTER).strip(), "pairs": len(out), "to_read": len(chosen),
                  "control_10926_14708": {"rank": out.index(control) if control else None, "read": bool(control and control["read"])},
                  "candidates": out}, indent=1))

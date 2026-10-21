"""Pairs of open PRs worth reading for a semantic clash: two PRs from different authors that meet on the same
concept without needing to share a file, where git and the checks in repo_audit.py see nothing. Anchors:
  - fhir resource   both change the same FHIR resource's services, controllers or routes;
  - table           both write the same table, or one writes it and the other reads it;
  - route family    both add routes under the same /api/<x> or /fhir/<X>;
  - behaviour       one changes a method and the other adds calls to it.
Access-control code, PRs labelled Security or ACL & Permissions, and PRs over the mass limit are left out.
Reads the audit's caches and git objects as text; runs nothing from any PR.

    python3 -I semantic_candidates.py <openemr clone> <cache dir> > candidates.json
"""
import json
import re
import subprocess
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

REPO, C = Path(sys.argv[1]), Path(sys.argv[2])
W = C / "audit"
MASTER = "upstream/master"
SKIP_LABELS = {"Security", "ACL & Permissions"}
ACL = re.compile(r"(?i)(^gacl/|/Acl/|\bacl\b|gacl_|AclMain|AclExtended|module_acl|users_secure|^users$|^groups$|Scope|Authoriz|OAuth|OpenIDConnect|oauth2/)")
# helpers every PR calls say nothing about a shared concept
INFRA = re.compile(r"(?i)(Logger|getLogger|get_id$|ProcessingResult|getInstance|Container|Helper|Utils?::|Trait::|translate|escape|xl$)")
TEST = re.compile(r"^(tests/|.*/tests?/)")
COMMON_METHODS = {"get", "set", "save", "render", "execute", "insert", "update", "delete", "search", "getAll", "getOne", "toArray",
                  "getId", "process", "handle", "run", "create", "load", "find", "getData", "getValue", "setValue", "validate",
                  "format", "parse", "build", "init", "setup", "getInstance", "getName", "getType", "getString", "getInt", "getBoolean",
                  "fetch", "query", "count", "exists", "add", "remove", "getResult", "getMessage", "isValid", "hasErrors"}


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).stdout


prs = {int(k): v for k, v in json.loads((W / "prs.json").read_text()).items()}
symbols = json.loads((W / "symbols.json").read_text())
findings = json.loads((W / "findings.json").read_text())
meta = {p["number"]: p for p in json.loads((C / "pr-meta.json").read_text())}
mass = {int(n) for n in findings["summary"]["mass"]}
skip = {n for n, m in meta.items() if SKIP_LABELS & {l["name"] for l in m.get("labels", [])}}
author = {n: m["author"]["login"] for n, m in meta.items()}
conflicting = {tuple(x["prs"]) for x in findings["text_conflict"]}
stacked = {tuple(sorted((x["contained"], x["in"]))) for x in findings["stacked"]}

RESOURCES = sorted({p.rsplit("/", 1)[-1][4:-4] for p in git("ls-tree", "--name-only", MASTER, "src/FHIR/R4/FHIRDomainResource/").split()
                    if p.endswith(".php")}, key=len, reverse=True)
TABLES = set(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)?\s+`?(\w+)`?", git("show", f"{MASTER}:sql/database.sql"), re.I))

WRITE = re.compile(r"(?i)\b(?:INSERT\s+(?:IGNORE\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+`?(\w+)`?")
READ = re.compile(r"(?i)\b(?:FROM|JOIN)\s+`?(\w+)`?")
ROUTE = re.compile(r"""['"](?:GET|POST|PUT|PATCH|DELETE) /(api|fhir|portal)/(\w+)""")
CALL = re.compile(r"(?:->|::)(\w+)\s*\(")


def resource_of(path):
    m = re.match(r"src/Services/FHIR/(\w+)/", path)
    if m and m.group(1) in RESOURCES:
        return m.group(1)
    base = path.rsplit("/", 1)[-1]
    if base.startswith("Fhir") and re.search(r"(Service|RestController|Validator|Serializer)\.php$", base):
        return next((r for r in RESOURCES if base[4:].startswith(r)), None)
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
            anchors[("fhir resource", r)][n].add("changes")
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
        if path.startswith("apis/routes/"):
            for kind, fam in ROUTE.findall(text):
                anchors[("route family", f"/{kind}/{fam}")][n].add("adds routes")
                if kind == "fhir" and fam in RESOURCES:
                    anchors[("fhir resource", fam)][n].add("adds routes")
    calls[n] = {path: set(CALL.findall(text)) for path, text in e.get("added_by_file", {}).items() if not TEST.match(path)}
    for cls, meth, path in changed_methods(e):
        if not ACL.search(path) and not ACL.search(cls) and not ACL.search(meth):
            anchors[("method", f"{cls}::{meth}")][n].add("changes")

pairs = defaultdict(list)
WEIGHT = {"fhir resource": 3, "route family": 2, "behaviour": 3}
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

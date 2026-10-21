# SPDX-License-Identifier: GPL-3.0-or-later
"""The path forward: what is left to do in each area, cut the way its plan says to cut it, and whether each piece is
already in an open PR, waiting on another issue, or free. Called from intent.py; reads master's tree and the plan
issues, and writes nothing itself.

Kinds of to-do, per the profile's streams:
  batches    a retired pattern (or file type) counted on master per folder; files an open PR changes are "busy",
             files that also write a superglobal are flagged for hand work, and folders the plan names come first
  keys       OEGlobalsBag keys the sub-issues move out, with call sites on master per sub-issue
  issues     plan issues as PR-sized items, with call sites where a pattern is given, and order between them
  checklist  a tracker issue's unticked files, with how many files on master mention each
"""
import re
import subprocess
from collections import defaultdict



def build(repo, master, streams, prs, per_file, mass_dirs, refs, gh_issue, profile):
    SCOPE = profile["code_files"]
    INVITED = set(profile["invited_labels"])
    LEFT_OUT = tuple(profile["left_out_paths"]) or ("\0",)  # paths never listed as work to pick up
    REACH = profile.get("reach_files") or profile["code_files"]  # where callers of a tracked file are counted

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout

    def counts(pattern, exclude=None):
        """path -> occurrences on master, by PCRE (every match, not matching lines)."""
        out = defaultdict(int)
        for line in git("grep", "-o", "-P", pattern, master, "--", *SCOPE).splitlines():
            path = line.split(":", 2)[1]  # "<sha>:<path>:<match>"
            if not (exclude and path.startswith(exclude)):
                out[path] += 1
        return dict(out)

    cites = defaultdict(set)  # issue -> open PRs that cite it (fix or mention), or are it
    for n, r in refs.items():
        for i in set(r.get("fixes", [])) | set(r.get("mentions", [])):
            cites[i].add(n)

    def status(issue, after=()):
        i = gh_issue(issue)
        state = i.get("state")
        if state == "closed":
            return {"state": "done"}
        if i.get("pr") and issue in prs:
            return {"state": "in flight", "prs": [issue]}
        open_prs = sorted(n for n in cites.get(issue, ()) if n in prs)
        if open_prs:
            return {"state": "in flight", "prs": open_prs}
        waiting = [a for a in after if gh_issue(a).get("state") != "closed"]
        if waiting:
            return {"state": "waiting", "on": waiting}
        return {"state": "free"}

    def item_meta(issue):
        i = gh_issue(issue)
        return {"issue": issue, "issue_title": i.get("title", ""), "owner": i.get("user"),
                "invited": bool(INVITED & set(i.get("labels", [])))}

    def group_of(path):
        return "/".join(path.split("/")[:-1][:3]) or "(root)"

    def busy(files):
        out = {}
        for f in files:
            if per_file.get(f):
                out[f] = sorted(per_file[f])
        return out

    def mass_in(group):
        return sorted(int(n) for n, dirs in mass_dirs.items() if any(group.startswith(d) or d.startswith(group) for d in dirs))

    result = {}
    for s in streams["streams"]:
        todos = []
        for t in s.get("todo", []):
            if t["kind"] == "batches":
                if t.get("files"):
                    names = [p for p in git("ls-tree", "-r", "--name-only", master).splitlines()
                             if re.search(t["files"], p) and "vendor/" not in p and not p.startswith(("tests/", "node_modules/"))]
                    per = {p: 1 for p in names}
                else:
                    per = counts(t["pattern"])
                writes = set(counts(t["flag_writes"])) if t.get("flag_writes") else set()
                req = counts(t["flag_request"]) if t.get("flag_request") else {}
                groups = defaultdict(dict)
                left_out = sum(c for p, c in per.items() if p.startswith(LEFT_OUT))
                per = {p: c for p, c in per.items() if not p.startswith(LEFT_OUT)}
                for p, c in per.items():
                    groups[group_of(p)][p] = c
                order = t.get("order", [])
                rank = lambda g: (next((i for i, o in enumerate(order) if g.startswith(o)), len(order)), sum(groups[g].values()))
                batches = []
                for g in sorted(groups, key=rank):
                    files = groups[g]
                    b = busy(files)
                    batches.append({"group": g, "count": sum(files.values()), "files": len(files),
                                    "busy": b, "free_count": sum(c for f, c in files.items() if f not in b),
                                    "writes": sorted(f for f in files if f in writes),
                                    "request": sum(req.get(f, 0) for f in files), "mass": mass_in(g),
                                    "planned_first": rank(g)[0] < len(order)})
                todos.append({"kind": "batches", "label": t["label"], **item_meta(t["issue"]), "says": t.get("says"),
                              "total": sum(per.values()), "total_files": len(per), "batches": batches, "left_out": left_out})
            elif t["kind"] == "keys":
                for k in s.get("keys_leaving", []):
                    keys = "|".join(re.escape(x) for x in k["keys"])
                    pat = rf"(?:->(?:get|getString|getInt|getBoolean|getAlpha|getAlnum|getDigits|has|set|remove)\(\s*|\$GLOBALS\[\s*)['\"]({keys})['\"]"
                    per = counts(pat)
                    todos.append({"kind": "item", "title": k.get("title") or f"Move {k['what']} out of OEGlobalsBag", **item_meta(k["issue"]),
                                  "keys": k["keys"], "sites": sum(per.values()), "site_files": len(per),
                                  "busy": len(busy(per)), **status(k["issue"])})
            elif t["kind"] == "issues":
                for it in t["items"]:
                    meta = item_meta(it["issue"])
                    rec = {"kind": "item", "title": it.get("title") or meta["issue_title"], **meta, **status(it["issue"], it.get("after", []))}
                    if it.get("pattern"):
                        per = counts(it["pattern"], it.get("exclude"))
                        rec.update({"sites": sum(per.values()), "site_files": len(per), "busy": len(busy(per))})
                    todos.append(rec)
            elif t["kind"] == "checklist":
                body = gh_issue(t["issue"]).get("body") or ""
                paths = re.findall(t["item_re"], body, re.M)
                ticked = len(re.findall(r"(?im)^\s*- \[x\]", body))
                rows = []
                citing = cites.get(t["issue"], set())
                left_out = [x for x in paths if x.startswith(LEFT_OUT)]
                for path in (x for x in paths if not x.startswith(LEFT_OUT)):
                    # reach: files that call the functions it defines (composer autoloads many of these files,
                    # so nobody has to name the file to use them)
                    fns = git("grep", "-hoP", r"^function\s+&?\K\w+", master, "--", path).split()
                    callers = []
                    if fns:
                        pat = r"(?<![\w>:$\\])(" + "|".join(map(re.escape, fns)) + r")\s*\("
                        callers = [p.split(":", 1)[1] for p in git("grep", "-l", "-P", pat, master, "--", *REACH).splitlines()]
                    # and files that include it by name, for the ones that define no functions
                    callers += [p.split(":", 1)[1] for p in git("grep", "-lF", path.rsplit("/", 1)[-1], master, "--", *REACH).splitlines()]
                    callers = sorted(set(callers))
                    changing = sorted(n for n in per_file.get(path, ()) if n in prs)
                    rows.append({"path": path, "functions": len(fns), "callers": len([c for c in callers if c != path]),
                                 "prs": [n for n in changing if n in citing], "busy": [n for n in changing if n not in citing],
                                 "exists": bool(git("ls-tree", master, "--", path).strip())})
                rows.sort(key=lambda r: (bool(r["prs"]), not r["exists"], bool(r["busy"]), r["callers"]))
                todos.append({"kind": "checklist", **item_meta(t["issue"]), "says": t.get("says"), "ticked": ticked,
                              "total": ticked + len(paths), "rows": rows, "left_out": len(left_out)})
        if todos:
            result[s["id"]] = todos
    return result

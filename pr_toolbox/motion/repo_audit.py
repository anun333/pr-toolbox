# SPDX-License-Identifier: GPL-3.0-or-later
"""Repository-wide conflict audit of every open PR against the others and the base branch ("master" below): the
pairs that conflict as text, and the pairs that merge cleanly but meet in the code:
  - text conflict: B merged onto (master + A) conflicts, split into mechanical files (the profile's "mechanical":
    version files, migrations, lint baselines) and code;
  - deleted, still changed: one deletes or moves a file the other changes;
  - same method: both change the same method without a text conflict;
  - removed, still used: one removes a function, class or method that master defines nowhere else, and the
    other's added lines call it;
  - signature: one changes a method's signature, and a class extending or implementing that class overrides it
    with a signature PHP would refuse, in another PR or on master in a file the first PR doesn't update
    (PHP only: the profile's "symbols");
  - shared names: both add the same name of a kind the profile lists (routes, tables, settings, events...).
It also lists, per PR, what a contributor building on master should know is in motion: base-class contracts
changed, public symbols removed, files moved or deleted. PRs touching more than MASS files (mass refactors,
regenerated code) are listed apart, since they meet everything.

Reads git objects and patches as text; runs nothing from them. Each PR's head must be fetched as refs/pr/<n>.

    WIM_PROFILE=<profile.json> python3 -I repo_audit.py <clone> <work dir> <pr list file: "number base" per line>
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pr_toolbox.common import caps_mismatches, conflicted_files, merged, version_claim  # noqa: E402

P = json.loads(Path(os.environ["WIM_PROFILE"]).read_text())

REPO, W = Path(sys.argv[1]), Path(sys.argv[2])
LIST = [l.split() for l in Path(sys.argv[3]).read_text().split("\n") if l.strip()]
NUMBERS = [int(x[0]) for x in LIST]
MASS = P["mass_files"]
MASTER = os.environ.get("WIM_MASTER") or f"{P['remote']}/{P['base_branch']}"  # WIM_MASTER pins a master, e.g. to replay a build
# Release branches merged up into the base after every change, oldest first (the profile's merge_up). A PR aimed at one
# takes its diff from that branch, merges onto it, and is merged up the chain; the comparisons with other PRs happen
# on master, where every branch's work ends up.
MERGE_UP = P.get("merge_up") or []
BASE_OF = {int(x[0]): (x[1] if len(x) > 1 and x[1] in MERGE_UP else None) for x in LIST}
MECHANICAL = re.compile(P["mechanical"])
W.mkdir(parents=True, exist_ok=True)
(W / "blobs").mkdir(exist_ok=True)


def git(*args, check=True):
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    if check and r.returncode not in (0, 1):
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr[:200]}")
    return r


MASTER_SHA = git("rev-parse", MASTER).stdout.strip()
SECONDS, _t = {}, [time.time()]


THREADS = max(1, int(os.environ.get("WIM_THREADS", "1")))
POOL = ThreadPoolExecutor(max_workers=THREADS)


def pmap(fn, items):
    """fn over items on THREADS threads (git does the work in subprocesses), results in order."""
    items = list(items)
    return list(POOL.map(fn, items)) if THREADS > 1 and len(items) > 1 else [fn(x) for x in items]


def read_blobs(rev, paths):
    """path -> text at rev, for many files through one git cat-file process."""
    paths = sorted(set(paths))
    if not paths:
        return {}
    data = subprocess.run(["git", "-C", str(REPO), "cat-file", "--batch"], input="".join(f"{rev}:{x}\n" for x in paths).encode(),
                          capture_output=True).stdout
    out, i = {}, 0
    for x in paths:
        nl = data.index(b"\n", i)
        header, i = data[i:nl].decode(), nl + 1
        if header.endswith("missing"):
            out[x] = ""
            continue
        size = int(header.split()[2])
        out[x] = data[i:i + size].decode("utf-8", "replace")
        i += size + 1
    return out


def mark(stage):
    """How long the stage that just ended took, for the summary."""
    now = time.time()
    SECONDS[stage] = round(now - _t[0], 1)
    _t[0] = now


# Names two PRs can both add are read from these paths' added lines, for every PR, large ones included.
NAME_PATHS = P["name_paths"]
# Patterns the plan retires, counted in each PR's added and removed lines (the profile's streams, "direction").
STREAMS = P["streams"]
DIRECTION = [d for s in STREAMS["streams"] for d in s.get("direction", [])]
# The cache key is the patterns themselves, so a corrected pattern recounts every PR, not only a new one.
DIRECTION_V = hashlib.sha256(json.dumps(DIRECTION, sort_keys=True).encode()).hexdigest()[:12]

# --- 1. Each PR's changes, against its merge base with master (or with its release branch); cached by the PR's head --
prs = {}
cache = W / "prs.json"
if cache.exists():
    prs = {int(k): v for k, v in json.loads(cache.read_text()).items()}
HEADS = {n: git("rev-parse", f"refs/pr/{n}").stdout.strip() for n in NUMBERS}
UP_TIP = {b: git("rev-parse", f"{P['remote']}/{b}").stdout.strip() for b in MERGE_UP}
BASES = {n: git("merge-base", UP_TIP[BASE_OF[n]] if BASE_OF[n] else MASTER, f"refs/pr/{n}").stdout.strip() for n in NUMBERS}
prs = {n: e for n, e in prs.items() if n in HEADS and e.get("head") == HEADS[n] and e.get("base") == BASES[n]
       and e.get("direction_v") == DIRECTION_V and "name_removed" in e}
for n in NUMBERS:
    if n in prs:
        continue
    head = f"refs/pr/{n}"
    base = BASES[n]
    status = git("diff", "--name-status", "-M", base, head).stdout.splitlines()
    files = {}
    for line in status:
        parts = line.split("\t")
        st = parts[0][0]
        old, new = (parts[1], parts[2]) if st == "R" else (parts[1], parts[1])
        files[new] = {"status": st, "old": old}
    entry = {"base": base, "head": HEADS[n], "files": files, "php": {}, "added_by_file": {}, "name_lines": {},
             "direction": {}, "direction_v": DIRECTION_V}
    for path, f in files.items():
        if not path.endswith(".php") or len(files) > MASS:
            continue
        info = {}
        if f["status"] in "MRD":
            info["base_blob"] = git("rev-parse", f"{base}:{f['old']}").stdout.strip()
        if f["status"] in "MRA":
            info["head_blob"] = git("rev-parse", f"{head}:{path}").stdout.strip()
        if f["status"] in "MR":
            hunks = git("diff", "-U0", base, head, "--", f["old"], path).stdout
            info["old_lines"] = [[int(a), int(b or 1)] for a, b in re.findall(r"^@@ -(\d+)(?:,(\d+))? \+", hunks, re.M)]
        entry["php"][path] = info
    # added lines per file, for calls to symbols other PRs remove
    if len(files) <= MASS:
        current = None
        for l in git("diff", "-U0", base, head, "--", "*.php").stdout.splitlines():
            if l.startswith("+++ "):
                current = l[6:] if l.startswith("+++ b/") else None
            elif l.startswith("+") and current:
                entry["added_by_file"].setdefault(current, []).append(l[1:])
        entry["added_by_file"] = {p: "\n".join(ls) for p, ls in entry["added_by_file"].items()}
    # added lines where shared names live (every PR: the largest ones add tables and routes too), and removed ones, so a
    # kind marked added_only can tell a name the PR adds from one whose line it only edits
    current, entry["name_removed"] = None, {}
    for l in git("diff", "-U0", base, head, "--", *NAME_PATHS).stdout.splitlines():
        if l.startswith(("+++ ", "--- ")):
            current = l[6:] if l.startswith("+++ b/") else None if l.startswith("+++ ") else current
        elif l.startswith("+") and current:
            entry["name_lines"].setdefault(current, []).append(l[1:])
        elif l.startswith("-") and current:
            entry["name_removed"].setdefault(current, []).append(l[1:])
    entry["name_lines"] = {p: "\n".join(ls) for p, ls in entry["name_lines"].items()}
    entry["name_removed"] = {p: "\n".join(ls) for p, ls in entry["name_removed"].items()}
    # what the PR adds and removes of each pattern the plan retires (tests and baselines aside)
    added, removed = [], []
    for l in git("diff", "-U0", base, head, "--", *(P.get("diff_files") or P["code_files"])).stdout.splitlines():
        if l.startswith("+") and not l.startswith("+++"):
            added.append(l)
        elif l.startswith("-") and not l.startswith("---"):
            removed.append(l)
    added, removed = "\n".join(added), "\n".join(removed)
    for d in DIRECTION:
        if d["kind"] == "files":
            a = sum(1 for p, f in files.items() if f["status"] == "A" and re.search(d["pattern"], p))
            r_ = sum(1 for p, f in files.items() if f["status"] == "D" and re.search(d["pattern"], f["old"]))
        else:
            a, r_ = len(re.findall(d["pattern"], added)), len(re.findall(d["pattern"], removed))
        if a or r_:
            entry["direction"][d["id"]] = [a, r_]
    prs[n] = entry
cache.write_text(json.dumps(prs))
mass = sorted(n for n, e in prs.items() if len(e["files"]) > MASS)
normal = [n for n in NUMBERS if n not in mass]

mark("pr diffs")
# --- 2. Symbols of every base and head version, by PHP's tokenizer (only blobs not read before) -----------
blobs = {i[k] for e in prs.values() for i in e["php"].values() for k in ("base_blob", "head_blob") if k in i} \
    if P["symbols"] == "php" else set()  # no symbol reader for the project's language: the symbol checks find nothing
symfile = W / "symbols.json"
symbols = json.loads(symfile.read_text()) if symfile.exists() else {}
todo = [sha for sha in blobs if f"{sha}.php" not in symbols]
if todo:
    batch = W / "blobs-new"
    batch.mkdir(exist_ok=True)
    for sha in todo:
        (batch / f"{sha}.php").write_text(git("cat-file", "-p", sha).stdout)
    # the pinned image when docker is there (the same tokenizer everywhere), else the local php
    if shutil.which("docker"):
        cmd = ["docker", "run", "--rm", "--cpus", "3", "--network", "none", "-u", f"{os.getuid()}:{os.getgid()}",
               "-v", f"{Path(__file__).parent}:/t:ro", "-v", f"{batch}:/f:ro", P["php_image"], "php", "-d", "memory_limit=2G",
               "/t/php_symbols.php", "/f"]
    else:
        cmd = ["php", "-d", "memory_limit=2G", str(Path(__file__).parent / "php_symbols.php"), str(batch)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"php_symbols.php failed: {r.stderr[:300]}")
    symbols.update(json.loads(r.stdout))
    symfile.write_text(json.dumps(symbols))
    for f in batch.iterdir():
        f.unlink()
    batch.rmdir()
missing = [sha for sha in blobs if f"{sha}.php" not in symbols]
if missing:
    raise SystemExit(f"{len(missing)} PHP versions have no symbols")
sym = lambda sha: symbols.get(f"{sha}.php", {}) if sha else {}


def units(s):
    """Class::method and function ranges of a file's symbols: [(name, start, end)]."""
    out = []
    for c in s.get("classes", []):
        for m in c["methods"]:
            out.append((f"{c['name']}::{m['name']}", m["start"], m["end"]))
    for fn in s.get("functions", []):
        out.append((f"{fn['name']}()", fn["start"], fn["end"]))
    return out


def defined(s):
    """Classes, methods and functions a file defines."""
    names = set()
    for c in s.get("classes", []):
        names.add(("class", c["name"]))
        for m in c["methods"]:
            names.add(("method", f"{c['name']}::{m['name']}"))
    for fn in s.get("functions", []):
        names.add(("function", fn["name"]))
    return names


def params(sig):
    """(parameters, required parameters, return type) of a signature as written: "($a, $b = 1): ?T"."""
    depth, cut = 0, len(sig)
    for i, ch in enumerate(sig):
        depth += ch == "("
        depth -= ch == ")"
        if ch == ")" and depth == 0:
            cut = i
            break
    inner, rest = sig[1:cut], sig[cut + 1:].strip()
    parts, depth, cur = [], 0, ""
    for ch in inner:
        depth += ch in "([{"
        depth -= ch in ")]}"
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts = [p.strip() for p in parts + [cur] if p.strip()]
    required = sum(1 for p in parts if "=" not in p and "..." not in p)
    return len(parts), required, rest[1:].strip() if rest.startswith(":") else ""


def refused(parent_sig, child_sig):
    """Why PHP would refuse child_sig overriding parent_sig, or None. Catches the plain cases only: a return type
    the child lacks or writes differently, fewer parameters, more required ones."""
    pn, _, pr = params(parent_sig)
    cn, cr, crt = params(child_sig)
    if pr and pr not in ("mixed", "void") and not crt:
        return f"the override declares no return type; the parent now returns {pr}"
    types = lambda t: {x.strip().lstrip("\\").split("\\")[-1].lower() for x in t.lstrip("?").split("|")} | ({"null"} if t.startswith("?") else set())
    if pr and crt and not types(crt) <= types(pr) | ({"mixed"} if "mixed" in types(pr) else set()):
        return f"the override returns {crt}, which is wider than the parent's new {pr}"
    if cn < pn:
        return f"the override takes {cn} parameters; the parent now takes {pn}"
    if cr > pn:
        return f"the override requires {cr} parameters; the parent now passes at most {pn}"
    return None


touched = defaultdict(set)   # n -> {(path, unit)}
removed = defaultdict(set)   # n -> {(kind, name, path)}
sigchanged = defaultdict(set)  # n -> {(class, method, path, old sig, new sig)}
subclass_methods = defaultdict(set)  # n -> {(parent, method, path, sig)}: methods a PR adds/changes in a subclass
for n in normal:
    base_defs, head_defs = {}, set()
    for path, info in prs[n]["php"].items():
        b, h = sym(info.get("base_blob")), sym(info.get("head_blob"))
        if b and info.get("old_lines"):
            us = units(b)
            for start, count in info["old_lines"]:
                end = start + max(count, 1) - 1
                hit = [u for u, s, e in us if s <= end and e >= start]
                for u in hit or ["(top-level)"]:
                    touched[n].add((path, u))
        if b and not h:
            for u, _, _ in units(b):
                touched[n].add((path, u))
        for d in defined(b):
            base_defs.setdefault(d, info and prs[n]["files"][path]["old"])
        head_defs |= defined(h)
        if b and h:
            bm = {(c["name"], m["name"]): m["signature"] for c in b.get("classes", []) for m in c["methods"]}
            for c in h.get("classes", []):
                for m in c["methods"]:
                    old = bm.get((c["name"], m["name"]))
                    if old is not None and old != m["signature"] and m["name"] != "__construct":
                        sigchanged[n].add((c["name"], m["name"], path, old, m["signature"]))
        if h:
            changed_units = {u for p, u in touched[n] if p == path}
            new_units = {u for u, _, _ in units(h)} - ({u for u, _, _ in units(b)} if b else set())
            for c in h.get("classes", []):
                parents = [p.split("\\")[-1] for p in c["extends"] + c["implements"]]
                for m in c["methods"]:
                    u = f"{c['name']}::{m['name']}"
                    if u in changed_units or u in new_units:
                        for p in parents:
                            subclass_methods[n].add((p, m["name"], path, m["signature"]))
    # removed from the PR as a whole: a class moved to another file in the same PR is not removed
    for (kind, name), path in base_defs.items():
        if (kind, name) not in head_defs:
            removed[n].add((kind, name, path))

mark("symbols")
# --- 3. Each PR onto master, then pairs; cached by master and the PRs' head commits ---------------------
VRULE, CRULE = P.get("version_claims"), P.get("exact_caps")  # the profile's rules; either may be absent


def alive(commit):
    """A cached probe commit still exists (git gc prunes unreferenced ones)."""
    return commit is None or subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{commit}^{{commit}}"]).returncode == 0


def caps(ref):
    if not CRULE:
        return set()
    try:
        return set(caps_mismatches(REPO, ref, CRULE))
    except Exception as e:  # a tree whose caps file can't be read is reported, not taken as clean
        return {f"(caps unreadable: {str(e)[:80]})"}


# When master moves, only PRs and pairs touching a file master changed are merged again. A three-way merge decides
# each file on its own, so a PR that touches none of those files has the same conflicts as before; each result keeps
# the master it was computed on, and a reused result is merged again (and compared) whenever its commit is needed.
onmaster_cache = W / "onmaster.json"
onmaster = json.loads(onmaster_cache.read_text()) if onmaster_cache.exists() else {}
prev_master = onmaster.get("master")
moved = set() if prev_master == MASTER_SHA else None  # files master changed since the cached results; None: unknown
if prev_master and prev_master != MASTER_SHA and alive(prev_master) \
        and subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", prev_master, MASTER_SHA]).returncode == 0:
    moved = set(git("diff", "--name-only", "--no-renames", prev_master, MASTER_SHA).stdout.split())
MODE = P.get("mechanical_conflicts") or "stop"  # results merged under another mode are merged again
if onmaster.get("mode", "stop") != MODE:
    moved = None
if moved is None:
    onmaster = {"master": MASTER_SHA, "prs": {}}
pr_paths = {n: set(prs[n]["files"]) | {f["old"] for f in prs[n]["files"].values()} for n in normal}
touches_moved = lambda *ns: bool(moved) and any(pr_paths[n] & moved for n in ns)
INCREMENTAL = {"master_from": prev_master, "master_to": MASTER_SHA, "files_changed_on_master": None if moved is None else len(moved),
               "prs_merged_again": 0, "prs_reused": 0, "pairs_merged_again": 0, "pairs_reused": 0, "rechecks": 0, "rule_violations": []}
RESOLVE_MECH = P.get("mechanical_conflicts") == "resolve"


def onto_master(n):
    """PR n merged onto master: (commit, conflicted files). With the profile's "mechanical_conflicts": "resolve", a PR
    whose every conflict is in a mechanical file still gets a commit, taking master's side of those hunks (-X ours), so
    its pairs are merged on master like anyone else's instead of branch to branch. On php-src a NEWS line conflicts with
    the branch after nearly every fix: that needs a rebase, not a decision, and the PR's pairs are about its code."""
    commit, files = merged(REPO, MASTER_SHA, f"refs/pr/{n}")
    if commit is None and RESOLVE_MECH and files and all(MECHANICAL.match(f) for f in files):
        r = git("merge-tree", "--write-tree", "-X", "ours", MASTER_SHA, f"refs/pr/{n}")
        lines = r.stdout.strip().splitlines()
        if r.returncode == 0 and lines:
            commit = git("commit-tree", lines[0], "-p", MASTER_SHA, "-p", f"refs/pr/{n}", "-m",
                         "pr-toolbox probe, mechanical files as master has them").stdout.strip()
    return commit, files


entries, todo = {}, []
for n in normal:
    key = f"{n}:{HEADS[n]}"
    e = onmaster["prs"].get(key)
    if e is not None:
        e.setdefault("master", prev_master)
    stale_and_touched = e is not None and e["master"] != MASTER_SHA and touches_moved(n)
    if e is None or stale_and_touched or (e["master"] == MASTER_SHA and not alive(e["commit"])):
        todo.append(n)
    else:
        INCREMENTAL["prs_reused"] += 1
        entries[key] = e
for n, (commit, files) in zip(todo, pmap(onto_master, todo)):
    entries[f"{n}:{HEADS[n]}"] = {"commit": commit, "conflicts": files, "master": MASTER_SHA}
    INCREMENTAL["prs_merged_again"] += 1
onmaster = {"master": MASTER_SHA, "mode": MODE, "prs": entries}


def refresh(ns):
    """Re-merge, in parallel, every result in ns reused from an earlier master, checking each agrees."""
    stale = [n for n in sorted(ns) if onmaster["prs"][f"{n}:{HEADS[n]}"]["master"] != MASTER_SHA
             or not alive(onmaster["prs"][f"{n}:{HEADS[n]}"]["commit"])]
    for n, (commit, files) in zip(stale, pmap(onto_master, stale)):
        e = onmaster["prs"][f"{n}:{HEADS[n]}"]
        INCREMENTAL["rechecks"] += 1
        if sorted(files) != sorted(e["conflicts"]):
            INCREMENTAL["rule_violations"].append({"pr": n, "was": e["conflicts"], "now": files})
        e.update({"commit": commit, "conflicts": files, "master": MASTER_SHA})


mark("each PR onto the base")
onto = {n: onmaster["prs"][f"{n}:{HEADS[n]}"]["commit"] for n in normal}  # truthy when its pairs can be merged on master
mconf = {n: set(onmaster["prs"][f"{n}:{HEADS[n]}"]["conflicts"]) for n in normal}
clean = {n: bool(onto[n]) and not mconf[n] for n in normal}  # merged with no conflict at all, mechanical files included


# --- 3b. A PR aimed at a release branch: onto that branch, then up the chain; the merge onto master above is the last
# step. Merging a branch up carries every change on it, so a PR that merges onto its own branch but not onto a later
# one leaves whoever merges up a conflict to resolve there: work, but not a reason to rebase. Cached by the PR's head
# and each branch's tip.
def conflicts_onto(rev, ref):
    r = git("merge-tree", "--write-tree", "--name-only", rev, ref)
    return conflicted_files(r.stdout.strip().splitlines()[1:]) if r.returncode == 1 else []


up_cache_f = W / "merge-up.json"
up_cache = json.loads(up_cache_f.read_text()) if up_cache_f.exists() else {}
up_jobs = [(n, b) for n in normal if BASE_OF[n] for b in MERGE_UP[MERGE_UP.index(BASE_OF[n]):]]
up_key = lambda n, b: f"{n}:{HEADS[n]}:{UP_TIP[b]}"
up_todo = [(n, b) for n, b in up_jobs if up_key(n, b) not in up_cache]
for (n, b), files in zip(up_todo, pmap(lambda t: conflicts_onto(UP_TIP[t[1]], f"refs/pr/{t[0]}"), up_todo)):
    up_cache[up_key(n, b)] = files
up_cache = {up_key(n, b): up_cache[up_key(n, b)] for n, b in up_jobs}
up_cache_f.write_text(json.dumps(up_cache))
merge_up = {n: {"base": BASE_OF[n], "path": [{"branch": b, "conflicts": up_cache[up_key(n, b)]} for b in MERGE_UP[MERGE_UP.index(BASE_OF[n]):]]
                + [{"branch": P["base_branch"], "conflicts": sorted(mconf[n])}]} for n in normal if BASE_OF[n]}
# commits on each branch that master doesn't have yet: while there are any, a PR's merge onto master carries them too
UP_PENDING = {b: int(git("rev-list", "--count", f"{MASTER_SHA}..{UP_TIP[b]}").stdout.strip() or 0) for b in MERGE_UP}
mark("merge-up")

findings = {"text_conflict": [], "stacked": [], "deleted_changed": [], "same_method": [], "removed_used": [],
            "removed_vs_master": [], "signature": [], "signature_vs_master": [], "shared_name": [],
            "version_claims": {}, "caps_alone": {}, "caps_pairs": []}
by_unit = defaultdict(set)
for n, ts in touched.items():
    for t in ts:
        by_unit[t].add(n)
pairs_same = defaultdict(set)
for t, ns in by_unit.items():
    if len(ns) > 1 and t[1] != "(top-level)":
        for a, b in combinations(sorted(ns), 2):
            pairs_same[(a, b)].add(f"{t[0]} {t[1]}")

# PHPStan's exact caps: each PR merged onto master alone, then clean pairs that both touch the caps or baselines
mark("setup")
MASTER_CAPS = caps(MASTER_SHA)
CAPS_DIR = CRULE["counted"].split("{name}")[0] if CRULE else None
touches_caps = {n for n in normal if CRULE and any(f == CRULE["file"] or f.startswith(CAPS_DIR) for f in prs[n]["files"])}

by_file = defaultdict(set)
for n in normal:
    for path, f in prs[n]["files"].items():
        by_file[path].add(n)
        by_file[f["old"]].add(n)
# With "pair_files": "code", pairs that share only mechanical files are not merged: they can only need a rebase, never
# a decision, and on php-src (NEWS alone is in 185 PRs) they were half of the 37,182 pairs and of the pair stage's time.
CODE_PAIRS = P.get("pair_files") == "code"
share = {(a, b) for path, ns in by_file.items() if not (CODE_PAIRS and MECHANICAL.match(path)) for a, b in combinations(sorted(ns), 2)}
pair_cache = W / "pairs.json"
pairs = json.loads(pair_cache.read_text()) if pair_cache.exists() else {}
pairs = pairs if (pairs.get("master") == MASTER_SHA or moved is not None) and pairs.get("mode", "stop") == MODE else {"master": MASTER_SHA, "pairs": {}}
fresh = {}


def pair_still_holds(a, b, old):
    """A pair's result from an earlier base still holds when the base changed none of the files both PRs change and
    neither PR's standing against the base changed. With both merging cleanly onto the base, a conflict between them
    can only be in a file they both change; a file only one changes merges as it does onto the base alone."""
    if old.get("master", pairs.get("master")) == MASTER_SHA:
        return True
    if moved is None:
        return False
    if old["how"] == "onto master":
        if not (onto[a] and onto[b]) or (pr_paths[a] & pr_paths[b] & moved):
            return False
        if a in touches_caps and b in touches_caps and any(f == CRULE["file"] or f.startswith(CAPS_DIR) for f in moved):
            return False  # caps count the baselines, which either PR may leave as the base has them
        return True
    # branch to branch: the two heads merged directly, less each PR's conflicts with the base
    return not (onto[a] and onto[b]) and not touches_moved(a) and not touches_moved(b)


todo_pairs = []
for a, b in sorted(share):
    key = f"{a}:{HEADS[a]},{b}:{HEADS[b]}"
    old = pairs["pairs"].get(key)
    if old is not None and pair_still_holds(a, b, old):
        old.setdefault("master", pairs.get("master"))
        fresh[key] = old
        INCREMENTAL["pairs_reused"] += 1
    else:
        todo_pairs.append((a, b, key))

# every commit the caps checks and pair merges start from must be today's master's: re-merge stale ones once
caps_prs = sorted(n for n in touches_caps if clean[n])
refresh(set(caps_prs) | {a for a, b, _ in todo_pairs if onto[a] and onto[b]})
commit_of = lambda n: onmaster["prs"][f"{n}:{HEADS[n]}"]["commit"]
for n, bad in zip(caps_prs, pmap(lambda n: sorted(caps(commit_of(n)) - MASTER_CAPS), caps_prs)):
    if bad:
        findings["caps_alone"][n] = bad
mark("caps alone")


def pair_entry(t):
    a, b, _ = t
    if onto[a] and onto[b]:
        commit, files = merged(REPO, commit_of(a), f"refs/pr/{b}")
        entry = {"how": "onto master", "files": files, "master": MASTER_SHA}
        if commit and a in touches_caps and b in touches_caps and clean[a] and clean[b]:
            alone = set(findings["caps_alone"].get(a, [])) | set(findings["caps_alone"].get(b, []))
            entry["caps"] = sorted(caps(commit) - MASTER_CAPS - alone)
        return entry
    # one needs a rebase first: merge the branches directly, and leave out what either conflicts on with master
    r = git("merge-tree", "--write-tree", "--name-only", f"refs/pr/{a}", f"refs/pr/{b}")
    files = conflicted_files(r.stdout.strip().splitlines()[1:]) if r.returncode == 1 else []
    return {"how": "branch to branch", "files": [f for f in files if f not in mconf[a] | mconf[b]], "master": MASTER_SHA}


for (a, b, key), entry in zip(todo_pairs, pmap(pair_entry, todo_pairs)):
    fresh[key] = entry
    INCREMENTAL["pairs_merged_again"] += 1
fresh = dict(sorted(fresh.items()))
pairs["pairs"] = fresh
pairs["master"], pairs["mode"] = MASTER_SHA, MODE
pair_cache.write_text(json.dumps(pairs))
onmaster_cache.write_text(json.dumps(onmaster))
for key, v in sorted(pairs["pairs"].items()):
    if v.get("caps"):
        a, b = (int(x.split(":")[0]) for x in key.split(","))
        findings["caps_pairs"].append({"prs": [a, b], "caps": v["caps"]})

# v_database: the number each PR's head claims, when it changes it; PRs changing SQL without changing it
mark("pairs")
master_claim = version_claim(REPO, MASTER_SHA, VRULE) if VRULE else None
claims, no_bump = {}, []
NO_BUMP = re.compile(P["no_bump_re"]) if P.get("no_bump_re") else None
for n in NUMBERS if VRULE else []:
    head_claim, base_claim = version_claim(REPO, f"refs/pr/{n}", VRULE), version_claim(REPO, prs[n]["base"], VRULE)
    if head_claim is not None and head_claim != base_claim:
        claims[n] = head_claim
    elif NO_BUMP and any(NO_BUMP.match(f) for f in prs[n]["files"]):
        no_bump.append(n)
by_claim = defaultdict(list)
for n, c in claims.items():
    by_claim[c].append(n)
findings["version_claims"] = {"master": master_claim, "claims": claims, "no_bump": sorted(no_bump),
                              "shared": {c: sorted(ns) for c, ns in by_claim.items() if len(ns) > 1},
                              "taken": sorted(n for n, c in claims.items() if master_claim is not None and c <= master_claim)}
# SQL changed in an earlier release's upgrade file: only sites upgrading from before that release run it (right for
# a fix to the old upgrade path; a change every site needs also belongs in the current file)
UF = P.get("upgrade_files")
findings["stale_upgrade"] = {"current": None, "prs": {}}
if UF:
    vfile = git("show", f"{MASTER_SHA}:{UF['version_file']}").stdout
    target = UF["join"].join(re.search(UF["version_re"].replace("{part}", k), vfile).group(1) for k in UF["parts"])
    findings["stale_upgrade"]["current"] = UF["glob"].replace("{version}", target)
    for n in NUMBERS:
        stale = sorted(p for p, f in prs[n]["files"].items() if f["status"] in "MA" and re.match(UF["pattern"], p)
                       and not p.endswith(UF["current"].replace("{version}", target)))
        if stale:
            findings["stale_upgrade"]["prs"][n] = stale
conflicts = {tuple(int(x.split(":")[0]) for x in k.split(",")): v for k, v in pairs["pairs"].items()}


def their_files(a, b, v):
    """A pair's conflicted files that are the two PRs' work. Merged onto master, that is every one. Merged branch to
    branch, the heads also carry the history between their bases, and where both sides of that history changed a file
    neither PR touches, git conflicts there too: php-src #7173 (on master, last rebased 2023-11) against #24188 (on
    PHP-8.2) conflicted in 95 files, CI configuration among them, that neither changes. A conflict about their work is in
    a file both change."""
    if v["how"] == "onto master":
        return v["files"]
    both = pr_paths[a] & pr_paths[b]
    return [f for f in v["files"] if f in both]


conflicts = {(a, b): {**v, "files": their_files(a, b, v)} for (a, b), v in conflicts.items()}

for (a, b), v in sorted(conflicts.items()):
    if v["files"]:
        code = [f for f in v["files"] if not MECHANICAL.match(f)]
        findings["text_conflict"].append({"prs": [a, b], "how": v["how"], "code": code,
                                          "mechanical": [f for f in v["files"] if MECHANICAL.match(f)]})
for (a, b), units_ in sorted(pairs_same.items()):
    if not conflicts.get((a, b), {}).get("files"):
        findings["same_method"].append({"prs": [a, b], "where": sorted(units_)})

mark("version claims and upgrade files")
# one PR built on another: the first's head is an ancestor of the second's, so merging the second merges both
heads = {}


def head_blobs(n):
    """path -> blob at the PR's head, for the files it changes (absent for deletions)."""
    if n not in heads:
        out = git("ls-tree", "-r", f"refs/pr/{n}", "--", *prs[n]["files"]).stdout if len(prs[n]["files"]) <= MASS else ""
        heads[n] = {l.split("\t", 1)[1]: l.split()[2] for l in out.splitlines()}
    return heads[n]


stacked_cache = W / "stacked.json"
stacked_seen = json.loads(stacked_cache.read_text()) if stacked_cache.exists() else {}
stacked_now = {}


def stacked_pair(t):
    a, b = t
    found = []
    for x, y in ((a, b), (b, a)):
        if subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", f"refs/pr/{x}", f"refs/pr/{y}"]).returncode == 0:
            found.append({"contained": x, "in": y, "how": "built on it"})
        else:
            # carries it by content: every file x changes has x's version in y (version files and baselines aside)
            xs = {p: s for p, s in head_blobs(x).items() if not MECHANICAL.match(p)}
            if xs and all(head_blobs(y).get(p) == s for p, s in xs.items()):
                found.append({"contained": x, "in": y, "how": "carries its files"})
    return found


todo_stacked = [(a, b) for a, b in sorted(share) if f"{a}:{HEADS[a]},{b}:{HEADS[b]}" not in stacked_seen]
for n in {x for t in todo_stacked for x in t}:
    head_blobs(n)  # fill the cache before threads read it
for (a, b), found in zip(todo_stacked, pmap(stacked_pair, todo_stacked)):
    stacked_seen[f"{a}:{HEADS[a]},{b}:{HEADS[b]}"] = found
for a, b in sorted(share):
    key = f"{a}:{HEADS[a]},{b}:{HEADS[b]}"
    stacked_now[key] = stacked_seen[key]
    findings["stacked"].extend(stacked_seen[key])
stacked_cache.write_text(json.dumps(stacked_now))

mark("stacked")
# files one PR deletes or moves away that another changes
for a in normal:
    gone = {f["old"]: (path if f["status"] == "R" else None) for path, f in prs[a]["files"].items() if f["status"] in "DR"}
    for b in normal:
        if b == a:
            continue
        hit = sorted(p for p, f in prs[b]["files"].items() if f["status"] == "M" and p in gone)
        if hit:
            findings["deleted_changed"].append({"deletes": a, "changes": b, "files": hit,
                                                "mechanical": all(MECHANICAL.match(p) for p in hit),
                                                "moved_to": {p: gone[p] for p in hit if gone[p]}})

mark("deleted and same method")
# removed symbols called in another PR's added lines, when master defines the name nowhere the remover leaves
def grep_lines(pattern, *paths):
    """(path, text) of each line on master matching an extended regex: "<sha>:<path>:<line>:<text>"."""
    out = []
    for l in git("grep", "-n", "-E", pattern, MASTER_SHA, "--", *paths).stdout.splitlines():
        parts = l.split(":", 3)
        if len(parts) == 4:
            out.append((parts[1], parts[3]))
    return out


# where master defines each class, and each function or method, by name: one pass instead of one search per name
DEFINED = {"class": defaultdict(set), "function": defaultdict(set)}
if P["symbols"] == "php":
    for path, text in grep_lines(r"^\s*(final\s+|abstract\s+|readonly\s+)*(class|interface|trait|enum)\s+[A-Za-z_][A-Za-z0-9_]*", "*.php"):
        m = re.match(r"^\s*(?:final\s+|abstract\s+|readonly\s+)*(?:class|interface|trait|enum)\s+(\w+)", text)
        if m:
            DEFINED["class"][m.group(1)].add(path)
    for path, text in grep_lines(r"function\s+&?[A-Za-z_][A-Za-z0-9_]*\s*\(", "*.php"):
        for m in re.finditer(r"function\s+&?(\w+)\s*\(", text):
            DEFINED["function"][m.group(1)].add(path)


def defined_elsewhere(kind, short, keep_out):
    return DEFINED["class" if kind == "class" else "function"].get(short, set()) - keep_out


def master_callers(wanted):
    """{(kind, symbol): files on master with a non-comment line using it}, from one search per kind, chunked."""
    shapes = {"class": (r"(new\s+|extends\s+|implements\s+|instanceof\s+|\\)({names})\b|\b({names})::",
                        lambda s: rf"(new\s+|extends\s+|implements\s+|instanceof\s+|\\){s}\b|\b{s}::"),
              "function": (r"(^|[^A-Za-z0-9_>:$\\])({names})\s*\(", lambda s: rf"(^|[^A-Za-z0-9_>:$\\]){s}\s*\("),
              "method": (r"({names})\s*\(", lambda s: rf"{s}\s*\(")}
    found = defaultdict(set)
    for kind, (shape, one) in shapes.items():
        symbols_ = sorted({s for k, s in wanted if k == kind})
        for i in range(0, len(symbols_), 150):
            chunk = symbols_[i:i + 150]
            rx = {s: re.compile(one(s)) for s in chunk}
            for path, text in grep_lines(shape.format(names="|".join(chunk)), "*.php", ":!.phpstan"):
                if re.match(r"\s*(\*|//|/\*|#)", text):
                    continue
                for s in chunk:
                    if s.split("::")[-1] in text and rx[s].search(text):
                        found[(kind, s)].add(path)
    return found


DEFS_OF = {n: {d for i in prs[n]["php"].values() for d in defined(sym(i.get("head_blob")))} for n in normal}
NONCLASS_SHORT = {n: {d.split("::")[-1] for k, d in ds if k != "class"} for n, ds in DEFS_OF.items()}
TEXT_AT = {}


def text_at(b, p):
    if (b, p) not in TEXT_AT:
        TEXT_AT[(b, p)] = git("show", f"refs/pr/{b}:{p}").stdout
    return TEXT_AT[(b, p)]


vs_master = []  # (a, kind, name, path, symbol searched, a_files): looked up together once the loop is done


for a in normal:
    a_files = {f["old"] for f in prs[a]["files"].values()} | set(prs[a]["files"])
    a_deleted = {f["old"] for f in prs[a]["files"].values() if f["status"] in "DR"}
    for kind, name, path in removed[a]:
        short = name.split("::")[-1]
        if kind == "method" and (len(short) < 8 or short.startswith("__")):
            continue
        if defined_elsewhere(kind, short, a_files):
            continue
        s = re.escape(short)
        if kind == "class":
            pat = re.compile(rf"(?:new\s+|extends\s+|implements\s+|instanceof\s+|\\){s}\b|\b{s}::|\b{s}\s+&?\$|\buse\s+[\w\\]*\\{s}\s*;|:\s*\??{s}\b")
        elif kind == "function":
            pat = re.compile(rf"(?<![\w>:$\\]){s}\s*\(")
        else:
            pat = re.compile(rf"(?:->|::){s}\s*\(")
        cls_short = name.split("::")[0] if kind == "method" else None
        for b in normal:
            if b == a or (kind, name) in DEFS_OF[b] or short in NONCLASS_SHORT[b]:
                continue
            where = []
            for p, text in prs[b].get("added_by_file", {}).items():
                if p in a_deleted or MECHANICAL.match(p) or short not in text or not pat.search(text):
                    continue
                # a method call counts only where the file names the class (an import, a type, a static call)
                if cls_short and not re.search(rf"\b{re.escape(cls_short)}\b", text_at(b, p)):
                    continue
                where.append(p)
            if where:
                findings["removed_used"].append({"removes": a, "uses": b, "symbol": name, "kind": kind, "in": path, "called_in": where})
        # master's callers the PR doesn't update: code added to master since the PR's base (searched below, in one go)
        a_head_methods = {d.split("::")[-1] for k, d in DEFS_OF[a] if k == "method"}
        if not (kind == "function" and short in a_head_methods):
            searched = f"{cls_short}::{short}" if kind == "method" else short
            vs_master.append((a, kind, name, path, searched, a_files))

CALLERS = master_callers({(k, s) for _, k, _, _, s, _ in vs_master})
for a, kind, name, path, searched, a_files in vs_master:
    callers = sorted(p for p in CALLERS.get((kind, searched), set()) if p not in a_files)
    if callers:
        findings["removed_vs_master"].append({"removes": a, "symbol": name, "kind": kind, "in": path, "master_callers": callers[:10],
                                              "count": len(callers)})

mark("removed symbols")
# signature changes against an override PHP would refuse: in another PR, or on master in a file the PR leaves alone
changed_classes = sorted({cls for a in normal for cls, _, _, _, _ in sigchanged[a]})
subclass_files = defaultdict(set)
for i in range(0, len(changed_classes), 150):
    chunk = changed_classes[i:i + 150]
    for path, text in grep_lines(rf"(extends|implements)\s+[^{{]*\b({'|'.join(chunk)})\b", "*.php"):
        for cls in chunk:
            if re.search(rf"(extends|implements)\s+[^{{]*\b{cls}\b", text):
                subclass_files[cls].add(path)
SUBCLASS_TEXT = read_blobs(MASTER_SHA, {p for ps in subclass_files.values() for p in ps})
for a in normal:
    a_files = set(prs[a]["files"])
    for cls, meth, path, old, new in sigchanged[a]:
        for b in normal:
            if b == a:
                continue
            for p, m, pp, sig in subclass_methods[b]:
                if p == cls and m == meth and pp not in a_files:
                    why = refused(new, sig)
                    if why:
                        findings["signature"].append({"changes": a, "overrides": b, "method": f"{cls}::{meth}", "new": new,
                                                      "override_in": pp, "override": sig, "why": why})
        for pp in sorted(subclass_files[cls]):
            if pp in a_files:
                continue
            text = SUBCLASS_TEXT.get(pp, "")
            mm = re.search(rf"function\s+&?{meth}\s*(\((?:[^()]|\([^()]*\))*\)\s*(?::\s*[^{{;]+?)?)\s*[{{;]", text, re.S)
            if mm:
                sig = re.sub(r"\s+", " ", mm.group(1)).strip()
                why = refused(new, sig)
                if why:
                    findings["signature_vs_master"].append({"changes": a, "method": f"{cls}::{meth}", "new": new,
                                                            "master_file": pp, "override": sig, "why": why})

mark("signatures")
# shared names, of the kinds the profile lists, from every PR's added lines in its name paths


def name_kind(spec):
    """A profile name kind as a function (path, line) -> names."""
    rx = re.compile(spec["pattern"], re.I if "i" in spec.get("flags", "") else 0)
    where, fmt, params = spec.get("where", {}), spec.get("format"), spec.get("params")

    def fn(f, l):
        if ("prefix" in where and not f.startswith(where["prefix"])) or ("suffix" in where and not f.endswith(where["suffix"])) \
                or ("equals" in where and f != where["equals"]) or ("contains" in where and where["contains"] not in f):
            return []
        out = []
        for m in rx.findall(l):
            v = fmt.format(*m) if fmt else (m if isinstance(m, str) else m[0])
            out.append(re.sub(params, ":param", v) if params else v)
        return out
    return fn


KINDS = {spec["kind"]: name_kind(spec) for spec in P.get("names", [])}
# a kind marked added_only counts a name only when the PR doesn't also remove it: editing a stub's signature or an
# opcode's handler line rewrites its name without adding one
ADDED_ONLY = {spec["kind"] for spec in P.get("names", []) if spec.get("added_only")}
names = defaultdict(lambda: defaultdict(set))
for n in NUMBERS:
    gone = defaultdict(set)
    for path, text in prs[n].get("name_removed", {}).items() if ADDED_ONLY else ():
        for line in text.splitlines():
            for kind in ADDED_ONLY:
                gone[kind].update(KINDS[kind](path, line))
    for path, text in prs[n]["name_lines"].items():
        for line in text.splitlines():
            for kind, fn in KINDS.items():
                for v in fn(path, line):
                    if v not in gone[kind]:
                        names[kind][v].add(n)
for kind, vs in names.items():
    for v, ns in vs.items():
        if len(ns) > 1:
            findings["shared_name"].append({"kind": kind, "name": v, "prs": sorted(ns)})

mark("shared names")
# --- 4. In motion: what each PR changes that others build on ---------------------------------------------
motion = {}
for n in normal:
    moved = {f["old"]: p for p, f in prs[n]["files"].items() if f["status"] == "R" and f["old"] != p}
    deleted = sorted(f["old"] for f in prs[n]["files"].values() if f["status"] == "D")
    contracts = sorted({f"{c}::{m}" for c, m, _, _, _ in sigchanged[n]})
    gone = sorted({name for kind, name, _ in removed[n] if kind in ("class", "function") or not name.split("::")[-1].startswith("__")})
    if moved or deleted or contracts or gone:
        motion[n] = {"moved": moved, "deleted": deleted, "contracts": contracts, "removed": gone}

mark("in motion")
summary = {"master": MASTER_SHA, "open_prs": len(NUMBERS), "mass": {n: len(prs[n]["files"]) for n in mass},
           "conflict_with_master": sorted(n for n in normal if not clean[n]), "pairs_sharing_a_file": len(share),
           "counts": {k: len(v) for k, v in findings.items()}, "incremental": INCREMENTAL, "seconds": SECONDS}
if MERGE_UP:
    summary["merge_up"] = {"branches": UP_TIP, "pending_into_base": UP_PENDING, "prs": len(merge_up),
                           "conflict_with_own_base": sorted(n for n, m in merge_up.items() if m["path"][0]["conflicts"])}
(W / "findings.json").write_text(json.dumps({"summary": summary, **findings, "motion": motion,
                                             "master_conflicts": {n: sorted(mconf[n]) for n in normal if mconf[n]},
                                             **({"merge_up": merge_up} if MERGE_UP else {})}, indent=1))
print(json.dumps({**summary, "conflict_with_master": len(summary["conflict_with_master"]),
                  **({"merge_up": {**summary["merge_up"], "conflict_with_own_base": len(summary["merge_up"]["conflict_with_own_base"])}} if MERGE_UP else {})}, indent=1))

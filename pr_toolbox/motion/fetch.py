# SPDX-License-Identifier: GPL-3.0-or-later
"""Fresh inputs for the work-in-motion map: every open PR's record from GitHub (title, author, draft,
labels, body, size, the issues GitHub links as closing, reviews with each reviewer's role, and the CI state of the
head commit), master and each PR's head fetched as refs/pr/<n>, and the plan issues named in streams.json.

    python3 -I fetch.py <clone> <cache dir>

Writes <cache>/open-prs.txt, pr-meta.json, pr-bodies.json, pr-extra.json and issues/<n>.json. Reads GitHub records
as data; runs nothing from them.
"""
import json
import re
import os
import subprocess
import sys
from pathlib import Path

REPO, C = Path(sys.argv[1]), Path(sys.argv[2])
C.mkdir(parents=True, exist_ok=True)
(C / "issues").mkdir(exist_ok=True)
HERE = Path(__file__).parent
P = json.loads(Path(os.environ["WIM_PROFILE"]).read_text())

QUERY = """query($cursor: String) {
  repository(owner: "%OWNER%", name: "%NAME%") { pullRequests(states: OPEN, first: 25, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { number title isDraft createdAt updatedAt baseRefName headRefOid additions deletions changedFiles body
      reviewDecision authorAssociation author { login }
      labels(first: 20) { nodes { name } }
      closingIssuesReferences(first: 10) { nodes { number } }
      reviews(last: 30) { nodes { author { login } authorAssociation state submittedAt } }
      comments { totalCount }
      commits(last: 1) { nodes { commit { oid statusCheckRollup { state contexts(first: 100) { totalCount nodes {
        __typename ... on CheckRun { name conclusion status } ... on StatusContext { context state } } } } } } } } } } }""".replace("%OWNER%", P["owner"]).replace("%NAME%", P["name"])

# CI is read the way pr-toolbox health reads it: checks the profile's health_ignore names are left out (Codecov by
# default), and a cancelled run is not a failure. GitHub's own rollup state counts both, so a PR whose only red is a
# coverage report would read as failing.
CI_IGNORE = re.compile(P.get("ci_ignore") or r"^codecov/")
# A check that must be there before CI counts as run. GitHub holds the workflows of a fork PR from a first-time
# contributor until a maintainer approves them; until then the head carries only checks such as an app's DCO, and
# nothing failing would read as passing.
CI_REQUIRED = re.compile(P["ci_required"]) if P.get("ci_required") else None
FAILED ={"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}
PENDING = {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}


def ci_state(rollup):
    """The head commit's CI state and failing checks. Falls back to GitHub's rollup state when it lists more
    checks than were fetched."""
    if not rollup:
        return None, []
    contexts = rollup.get("contexts") or {}
    nodes = contexts.get("nodes") or []
    if contexts.get("totalCount", 0) > len(nodes):
        return rollup.get("state"), []
    failing, pending, counted, required_seen = [], 0, 0, CI_REQUIRED is None
    for ctx in nodes:
        name = ctx.get("name") or ctx.get("context") or "?"
        if CI_IGNORE.search(name):
            continue
        counted += 1
        required_seen = required_seen or bool(CI_REQUIRED.search(name))
        state = (ctx.get("conclusion") or ctx.get("state") or ctx.get("status") or "").upper()
        if state in FAILED:
            failing.append(name)
        elif state != "CANCELLED" and (state in PENDING or (ctx.get("status") or "").upper() in PENDING):
            pending += 1
    if failing:
        return "FAILURE", sorted(set(failing))
    if not required_seen:
        return None, []
    return ("PENDING" if pending else "SUCCESS" if counted else rollup.get("state")), []


def run(*args, check=True):
    r = subprocess.run(list(args), capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"{' '.join(args[:4])}: {r.stderr.strip()[:300]}")
    return r.stdout


prs, cursor = [], None
while True:
    args = ["gh", "api", "graphql", "-f", f"query={QUERY}"] + (["-f", f"cursor={cursor}"] if cursor else [])
    page = json.loads(run(*args))["data"]["repository"]["pullRequests"]
    prs += page["nodes"]
    if not page["pageInfo"]["hasNextPage"]:
        break
    cursor = page["pageInfo"]["endCursor"]
if len(prs) < 50:
    raise SystemExit(f"only {len(prs)} open PRs came back; refusing to build on that")

meta, bodies, extra = [], [], {}
for p in prs:
    login = (p.get("author") or {}).get("login") or "ghost"
    labels = [{"name": l["name"]} for l in p["labels"]["nodes"]]
    meta.append({"number": p["number"], "title": p["title"], "isDraft": p["isDraft"], "createdAt": p["createdAt"],
                 "updatedAt": p["updatedAt"], "author": {"login": login}, "labels": labels, "baseRefName": p["baseRefName"]})
    bodies.append({"number": p["number"], "body": p["body"] or "", "reviewDecision": p["reviewDecision"]})
    commit = (p["commits"]["nodes"] or [{}])[0].get("commit") or {}
    extra[p["number"]] = {
        "head": p["headRefOid"], "additions": p["additions"], "deletions": p["deletions"], "files": p["changedFiles"],
        "author_role": p["authorAssociation"], "comments": p["comments"]["totalCount"],
        "closing": [n["number"] for n in p["closingIssuesReferences"]["nodes"]],
        "reviews": [{"by": (r.get("author") or {}).get("login") or "ghost", "role": r["authorAssociation"], "state": r["state"],
                     "at": (r["submittedAt"] or "")[:10]} for r in p["reviews"]["nodes"]],
    }
    on_head = commit.get("oid") == p["headRefOid"]
    extra[p["number"]]["ci"], extra[p["number"]]["ci_failing"] = ci_state(commit.get("statusCheckRollup")) if on_head else (None, [])
(C / "pr-meta.json").write_text(json.dumps(meta))
(C / "pr-bodies.json").write_text(json.dumps(bodies))
(C / "pr-extra.json").write_text(json.dumps(extra))
(C / "open-prs.txt").write_text("".join(f"{p['number']} {p['baseRefName']}\n" for p in prs))

# master and every open PR's head; a PR whose head can't be fetched is reported, not silently dropped
run("git", "-C", str(REPO), "fetch", "-q", P["remote"], P["base_branch"])
numbers = [p["number"] for p in prs]
for i in range(0, len(numbers), 40):
    run("git", "-C", str(REPO), "fetch", "-q", P["remote"], *[f"+refs/pull/{n}/head:refs/pr/{n}" for n in numbers[i:i + 40]])
missing = [n for n in numbers if run("git", "-C", str(REPO), "rev-parse", "-q", "--verify", f"refs/pr/{n}", check=False).strip() != extra[n]["head"]]
if missing:
    raise SystemExit(f"PR heads not fetched or not matching GitHub: {missing[:20]}")

# who merges: everyone who merged a PR in the past year, by quarter (search returns at most 1,000 per query)
from datetime import date, timedelta
MERGED = """query($q: String!, $cursor: String) { search(query: $q, type: ISSUE, first: 100, after: $cursor) {
  issueCount pageInfo { hasNextPage endCursor } nodes { ... on PullRequest { mergedBy { login } } } } }"""
mergers = {}


def merged_between(start, end):
    """Counts each merger in [start, end], halving the window while search would cut it off at 1,000."""
    q, cursor = f"repo:{P['github']} is:pr is:merged merged:{start}..{end}", None
    while True:
        args = ["gh", "api", "graphql", "-f", f"query={MERGED}", "-f", f"q={q}"] + (["-f", f"cursor={cursor}"] if cursor else [])
        page = json.loads(run(*args))["data"]["search"]
        if page["issueCount"] > 1000:
            if start == end:
                raise SystemExit(f"{q}: over 1,000 merges in one day")
            mid = start + (end - start) // 2
            merged_between(start, mid)
            merged_between(mid + timedelta(days=1), end)
            return
        for node in page["nodes"]:
            login = (node.get("mergedBy") or {}).get("login")
            if login:
                mergers[login] = mergers.get(login, 0) + 1
        if not page["pageInfo"]["hasNextPage"]:
            return
        cursor = page["pageInfo"]["endCursor"]


merged_between(date.today() - timedelta(days=365), date.today())
(C / "mergers.json").write_text(json.dumps(dict(sorted(mergers.items(), key=lambda x: -x[1]))))

# plan issues, fresh each build
streams = P["streams"]
plan = sorted({n for s in streams["streams"] for n in s["plan"]} | {i.get("issue") for s in streams["streams"] for i in s.get("keys_leaving", []) + s.get("removals", [])} - {None})
for n in plan:
    out = run("gh", "api", f"repos/{P['github']}/issues/{n}", "--jq",
              "{number,title,state,user:.user.login,updated_at,labels:[.labels[].name],body,comments,pr:(.pull_request!=null)}", check=False)
    (C / "issues" / f"{n}.json").write_text(out or "{}")
# conformance: the open issues each standard's query finds (and the project-wide one), and the issues and PRs its
# known gaps cite, fresh each build
conf = P.get("conformance") or {}
reported = {}
for key, q in [(s["id"], s.get("issues")) for s in conf.get("specs", [])] + [("_general", conf.get("issues"))]:
    if not q:
        continue
    query = f"repo:{P['github']} is:issue is:open " + (f'label:"{q["label"]}"' if q.get("label") else q["query"])
    out = run("gh", "api", "-X", "GET", "search/issues", "-f", f"q={query}", "-f", "per_page=5", "-f", "sort=updated",
              "--jq", "{total: .total_count, items: [.items[] | {number, title}]}", check=False)
    reported[key] = {"query": query, "label": q.get("label"), **(json.loads(out) if out.strip() else {"total": None, "items": []})}
(C / "conformance-issues.json").write_text(json.dumps(reported))
cited = sorted({int(m.group(1)) for s in conf.get("specs", []) for k in s.get("known", [])
                for m in [re.search(r"/(?:issues|pull)/(\d+)", k.get("url") or "")] if m})
for n in cited:
    out = run("gh", "api", f"repos/{P['github']}/issues/{n}", "--jq",
              "{number,title,state,user:.user.login,updated_at,labels:[.labels[].name],body,comments,pr:(.pull_request!=null)}", check=False)
    (C / "issues" / f"{n}.json").write_text(out or "{}")
print(json.dumps({"open_prs": len(prs), "drafts": sum(p["isDraft"] for p in prs), "heads_fetched": len(numbers) - len(missing),
                  "ci": {s: sum(1 for e in extra.values() if e["ci"] == s) for s in ("SUCCESS", "FAILURE", "ERROR", "PENDING", "EXPECTED", None)},
                  "plan_issues": len(plan), "mergers_past_year": mergers}))

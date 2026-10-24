# SPDX-License-Identifier: GPL-3.0-or-later
"""Fresh inputs for the work-in-motion map: every open PR's record from GitHub (title, author, draft,
labels, body, size, the issues GitHub links as closing, reviews with each reviewer's role, and the CI state of the
head commit), master and each PR's head fetched as refs/pr/<n>, and the plan issues named in streams.json.

    python3 -I fetch.py <clone> <cache dir>

Writes <cache>/open-prs.txt, pr-meta.json, pr-bodies.json, pr-extra.json and issues/<n>.json for the PRs aimed at
the base, and other-bases.json naming the rest. Reads GitHub records as data; runs nothing from them.
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
    nodes { number title isDraft createdAt updatedAt baseRefName headRefOid headRepository { url } additions deletions changedFiles body
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


def read_pages(call, size=25, least=5):
    """Every node of a paged GraphQL connection; call(size, cursor) -> (returncode, stdout, stderr). A page of PRs with
    many checks each can take GitHub past its 10-second limit (a 502, a 504, or a reply cut off mid-stream), so a page
    that fails is asked for again at half the size, down to `least`."""
    nodes, cursor = [], None
    while True:
        code, out, err = call(size, cursor)
        if code and size > least:
            size = max(least, size // 2)
            continue
        if code:
            raise SystemExit(f"gh api graphql: {err.strip()[:300]}")
        page = json.loads(out)["data"]["repository"]["pullRequests"]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = page["pageInfo"]["endCursor"]


def split_by_base(prs, base, merge_up=()):
    """(PRs aimed at the base, the rest). A PR aimed at another branch, such as a release branch's backport, merges onto
    that branch: merged onto the base it would carry every commit between the two, and meet everything. A branch the
    profile lists in merge_up is merged up into the base after every change (php-src's PHP-8.x branches), so its PRs end
    up on the base too and are kept."""
    kept = {base, *merge_up}
    return [p for p in prs if p["baseRefName"] in kept], [p for p in prs if p["baseRefName"] not in kept]


def _query(size, cursor):
    args = ["gh", "api", "graphql", "-f", f"query={QUERY.replace('first: 25', f'first: {size}', 1)}"] + (["-f", f"cursor={cursor}"] if cursor else [])
    r = subprocess.run(args, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


prs = read_pages(_query)
if len(prs) < 50:
    raise SystemExit(f"only {len(prs)} open PRs came back; refusing to build on that")
MERGE_UP = P.get("merge_up") or []
prs, others = split_by_base(prs, P["base_branch"], MERGE_UP)
(C / "other-bases.json").write_text(json.dumps([{"number": p["number"], "title": p["title"], "base": p["baseRefName"],
                                                 "by": (p.get("author") or {}).get("login") or "ghost"} for p in others]))

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

def fetch_head(repo, remote, n, head, head_repo=None):
    """refs/pr/<n> at the PR's head, from GitHub's refs/pull/<n>/head. That ref can be missing or behind the head GitHub
    reports for the PR (php-src on 2026-10-24: #24439 had only refs/pull/24439/merge; #21778's head ref sat at an
    older commit than the last one in its commit list). Then the head is read from the merge ref's second parent, or
    fetched by its id from the repository, which serves its forks' commits, or from the head repository itself. A
    route counts only when it comes out at the head GitHub reports."""
    git_ = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    at_head = lambda ref: git_("rev-parse", "-q", "--verify", ref).stdout.strip() == head
    if not git_("fetch", "-q", remote, f"+refs/pull/{n}/head:refs/pr/{n}").returncode and at_head(f"refs/pr/{n}"):
        return True
    if not git_("fetch", "-q", remote, f"+refs/pull/{n}/merge:refs/pr-merge/{n}").returncode:
        second_is_head = at_head(f"refs/pr-merge/{n}^2")
        git_("update-ref", "-d", f"refs/pr-merge/{n}")
        if second_is_head:
            return not git_("update-ref", f"refs/pr/{n}", head).returncode
    for source in (remote, head_repo):
        if source and not git_("fetch", "-q", source, head).returncode and at_head(f"{head}^{{commit}}"):
            return not git_("update-ref", f"refs/pr/{n}", head).returncode
    return False


# master and every open PR's head; a PR whose head can't be fetched is reported, not silently dropped
run("git", "-C", str(REPO), "fetch", "-q", P["remote"], P["base_branch"], *MERGE_UP)
numbers = [p["number"] for p in prs]
head_repo = {p["number"]: (p.get("headRepository") or {}).get("url") for p in prs}
for i in range(0, len(numbers), 40):
    batch = numbers[i:i + 40]
    r = subprocess.run(["git", "-C", str(REPO), "fetch", "-q", P["remote"], *[f"+refs/pull/{n}/head:refs/pr/{n}" for n in batch]],
                       capture_output=True, text=True)
    # one ref GitHub doesn't have fails the whole batch, and one it has kept behind fetches the wrong commit: those go
    # one by one
    at = lambda n: run("git", "-C", str(REPO), "rev-parse", "-q", "--verify", f"refs/pr/{n}", check=False).strip()
    for n in batch if r.returncode else [n for n in batch if at(n) != extra[n]["head"]]:
        fetch_head(REPO, P["remote"], n, extra[n]["head"], head_repo[n])
missing =[n for n in numbers if run("git", "-C", str(REPO), "rev-parse", "-q", "--verify", f"refs/pr/{n}", check=False).strip() != extra[n]["head"]]
if missing:
    raise SystemExit(f"PR heads not fetched or not matching GitHub: {missing[:20]}")

# who merges: everyone who merged a PR in the past year, by quarter (search returns at most 1,000 per query)
from datetime import date, timedelta
MERGED = """query($q: String!, $cursor: String) { search(query: $q, type: ISSUE, first: 100, after: $cursor) {
  issueCount pageInfo { hasNextPage endCursor } nodes { ... on PullRequest { mergedBy { login } } } } }"""
# Projects that land a PR by pushing its commits close it with that commit, unmerged (php-src: 33 of its last 100 closed
# PRs on 2026-10-24, against 29 merged with the button). With "landings": "merge+commit" those count too, by the
# commit's committer: the one who pushed it.
CLOSED_BY_COMMIT = """query($q: String!, $cursor: String) { search(query: $q, type: ISSUE, first: 100, after: $cursor) {
  issueCount pageInfo { hasNextPage endCursor } nodes { ... on PullRequest { timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {
    nodes { ... on ClosedEvent { closer { __typename ... on Commit { committer { user { login } } } } } } } } } } }"""
mergers = {}


def landed_by(node, kind):
    """Who landed a PR the search found: the merger, or for one closed by a commit, that commit's committer."""
    if kind == "merged":
        return (node.get("mergedBy") or {}).get("login")
    closer = (((node.get("timelineItems") or {}).get("nodes") or [{}])[-1] or {}).get("closer") or {}
    return ((closer.get("committer") or {}).get("user") or {}).get("login") if closer.get("__typename") == "Commit" else None


def merged_between(start, end, tries=3, kind="merged"):
    """Counts each merger in [start, end], halving the window while search would cut it off at 1,000. Under load, search
    can also end a window early with no error, on a page whose count reads 0, the first page included (decidim/decidim
    on 2026-10-22: 400 of a window's merges read; 100 of 420 on another). A read is whole when every page gives the same
    count and that many merges came back; one that isn't is read again, then halved, and a single day that stays short
    stops the build. Counts join the total only once their window is whole. kind "closed" reads the PRs closed unmerged
    instead, and counts those a commit closed."""
    what = f"is:merged merged:{start}..{end}" if kind == "merged" else f"is:closed is:unmerged closed:{start}..{end}"
    q, cursor, expected, seen, found, counts = f"repo:{P['github']} is:pr {what}", None, None, 0, {}, set()
    while True:
        args = ["gh", "api", "graphql", "-f", f"query={MERGED if kind == 'merged' else CLOSED_BY_COMMIT}", "-f", f"q={q}"] + (["-f", f"cursor={cursor}"] if cursor else [])
        page = json.loads(run(*args))["data"]["search"]
        if expected is None:
            if page["issueCount"] > 1000:
                if start == end:
                    raise SystemExit(f"{q}: over 1,000 merges in one day")
                mid = start + (end - start) // 2
                merged_between(start, mid, kind=kind)
                merged_between(mid + timedelta(days=1), end, kind=kind)
                return
            expected = page["issueCount"]
        counts.add(page["issueCount"])
        seen += len(page["nodes"])
        for node in page["nodes"]:
            login = landed_by(node, kind)
            if login:
                found[login] = found.get(login, 0) + 1
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    if seen != expected or len(counts) > 1:
        if tries > 1:
            return merged_between(start, end, tries - 1, kind)
        if start == end:
            raise SystemExit(f"{q}: search returned {seen} of {expected} merges")
        mid = start + (end - start) // 2
        merged_between(start, mid, kind=kind)
        merged_between(mid + timedelta(days=1), end, kind=kind)
        return
    for login, k in found.items():
        mergers[login] = mergers.get(login, 0) + k


merged_between(date.today() - timedelta(days=365), date.today())
if P.get("landings") == "merge+commit":
    merged_between(date.today() - timedelta(days=365), date.today(), kind="closed")
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


def quote_source_for(url, sources):
    """The profile's quote source that names url, and its match; (None, None) when none does."""
    for s in sources:
        m = re.match(s["match"], url or "")
        if m:
            return s, m
    return None, None


def quote_raw_url(url, sources):
    """The plain-text address of a quoted document outside the repository, by the profile's quote_sources (each a
    "match" regex and a "raw" template filled with its groups), or None. php-src's plans live on wiki.php.net, whose
    ?do=export_raw is the page as written; the rendered page is not what to match a quote against."""
    s, m = quote_source_for(url, sources)
    return s["raw"].format(*m.groups()) if s else None


def clean_quoted(text, source):
    """The source's own markup taken out by its "clean" pairs (regex, replacement), so a quote matches the words as a
    reader sees them: in DokuWiki, [[rfc:x|the label]] reads as its label and ''code'' as code."""
    for pat, rep in source.get("clean", []):
        text = re.sub(pat, rep, text)
    return text


# quoted documents outside the repository, fresh each build, so a --skip-fetch rebuild checks quotes offline. A source
# the profile names that can't be read stops the build: read as "no source", its quotes would go unchecked.
import hashlib
import urllib.request
QUOTE_SOURCES = P.get("quote_sources") or []
quoted = {}
urls = {t.get("url") for s in streams["streams"] for t in s.get("target", [])} | {g.get("url") for g in streams.get("general", [])}
for url in sorted(u for u in urls if u):
    source, _ = quote_source_for(url, QUOTE_SOURCES)
    raw = quote_raw_url(url, QUOTE_SOURCES)
    if not raw:
        continue
    req = urllib.request.Request(raw, headers={"User-Agent": "pr-toolbox motion (quote check)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", "replace")
    except OSError as e:
        raise SystemExit(f"quote source {raw}: {e}")
    if not text.strip():
        raise SystemExit(f"quote source {raw}: empty")
    (C / "quotes").mkdir(exist_ok=True)
    name = hashlib.sha256(url.encode()).hexdigest()[:16] + ".txt"
    (C / "quotes" / name).write_text(clean_quoted(text, source))
    quoted[url] = name
(C / "quotes.json").write_text(json.dumps(quoted, indent=1))
print(json.dumps({"open_prs": len(prs), "other_bases": len(others), "drafts": sum(p["isDraft"] for p in prs), "heads_fetched": len(numbers) - len(missing),
                  "on_release_branches": {b: sum(1 for p in prs if p["baseRefName"] == b) for b in MERGE_UP},
                  "ci": {s: sum(1 for e in extra.values() if e["ci"] == s) for s in ("SUCCESS", "FAILURE", "ERROR", "PENDING", "EXPECTED", None)},
                  "plan_issues": len(plan), "quoted_documents": len(quoted), "mergers_past_year": mergers}))

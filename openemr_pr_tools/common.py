# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared helpers: git and gh calls, open pull requests with their files, in-memory merges,
the database version a tree claims, and OpenEMR's exact PHPStan baseline caps."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

GITHUB_REPO = os.environ.get("OPENEMR_GITHUB_REPO", "openemr/openemr")
CAPS_FILE = ".phpstan/fatal-baseline-caps.php"
VERSION_FILES = frozenset({"version.php", "sql/database.sql"})
UPGRADE_FILE = re.compile(r"^sql/\d+_\d+_\d+-to-\d+_\d+_\d+_upgrade\.sql$")

PRS_QUERY = """query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) { pullRequests(states: OPEN, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { number title isDraft author { login } headRefOid body files(first: 100) { totalCount nodes { path } } } } } }"""


class ToolError(RuntimeError):
    """A command the tool depends on failed, so its answer can't be trusted."""


def repo_dir(path: str | None = None) -> Path:
    return Path(path or os.environ.get("OPENEMR_REPO", Path.home() / "dev" / "openemr")).expanduser()


def git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise ToolError(f"git {' '.join(args[:3])}: {result.stderr.strip()[:300]}")
    return result


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise ToolError(f"gh {' '.join(args[:3])}: {result.stderr.strip()[:300]}")
    return result.stdout


def gh_json(*args: str) -> dict | list:
    return json.loads(gh(*args))


def current_login() -> str:
    return gh("api", "user", "--jq", ".login").strip()


def open_prs() -> list[dict]:
    """Every open PR with its complete list of changed files. GraphQL lists the first 100
    files, so larger PRs get theirs from the REST files endpoint."""
    owner, name = GITHUB_REPO.split("/")
    prs, cursor = [], None
    while True:
        args = ["api", "graphql", "-f", f"query={PRS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        page = gh_json(*args)["data"]["repository"]["pullRequests"]
        prs += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    for pr in prs:
        pr["author"] = (pr.get("author") or {}).get("login", "")
        pr["paths"] = {f["path"] for f in pr["files"]["nodes"]}
        if pr["files"]["totalCount"] > len(pr["paths"]):
            listing = gh("api", "--paginate", f"repos/{GITHUB_REPO}/pulls/{pr['number']}/files?per_page=100", "--jq", ".[].filename")
            pr["paths"] = set(listing.split())
    return prs


def fetch_prs(repo: Path, numbers: list[int], remote: str = "upstream") -> None:
    """Fetches master and each PR's head as refs/remotes/<remote>/pr/<number>."""
    git(repo, "fetch", "-q", remote, "master", check=True)
    for i in range(0, len(numbers), 40):
        refs = [f"+pull/{n}/head:refs/remotes/{remote}/pr/{n}" for n in numbers[i:i + 40]]
        git(repo, "fetch", "-q", remote, *refs)


def merged(repo: Path, base: str, ref: str) -> tuple[str | None, list[str]]:
    """ref merged onto base in memory: the merge commit, or None and the conflicted files."""
    result = git(repo, "merge-tree", "--write-tree", "--name-only", base, ref)
    lines = result.stdout.strip().splitlines()
    if result.returncode == 1:
        return None, conflicted_files(lines[1:])
    if result.returncode != 0 or not lines:
        raise ToolError(f"merge-tree {ref}: {result.stderr.strip()[:200]}")
    commit = git(repo, "commit-tree", lines[0], "-p", base, "-p", ref, "-m", "openemr-pr-tools probe", check=True).stdout.strip()
    return commit, []


def conflicted_files(lines: list[str]) -> list[str]:
    """The file names `git merge-tree --name-only` lists before its messages."""
    return sorted({line for line in lines if line and not line.startswith(("Auto-merging", "CONFLICT"))})


def show(repo: Path, ref: str, path: str) -> str | None:
    result = git(repo, "show", f"{ref}:{path}")
    return result.stdout if result.returncode == 0 else None


def v_database(repo: Path, ref: str) -> int | None:
    """The database version a tree claims in version.php."""
    match = re.search(r"\$v_database = (\d+);", show(repo, ref, "version.php") or "")
    return int(match.group(1)) if match else None


def caps_mismatches(repo: Path, ref: str) -> list[str]:
    """Each exact ('all') PHPStan baseline cap whose file holds a different number of entries.
    Two PRs that each lower the same cap pass alone and fail together; comparing a merged tree
    with each PR alone shows that."""
    source = show(repo, ref, CAPS_FILE)
    if source is None:
        return []
    block = re.search(r"'all' => \[(.*?)\]", source, re.S)
    if block is None:
        raise ToolError(f"no 'all' block in {CAPS_FILE} at {ref}")
    bad = []
    for name, cap in re.findall(r"'([\w.]+\.php)' => (\d+)", block.group(1)):
        entries = (show(repo, ref, f".phpstan/baseline/{name}") or "").count("$ignoreErrors[] = [")
        if entries != int(cap):
            bad.append(f"{name.removesuffix('.php')}: cap {cap}, entries {entries}")
    return bad


def is_version_conflict(files: list[str]) -> bool:
    """Whether a conflict is only the database-version kind: version.php, database.sql's header
    and the end of an upgrade script, which the PR that lands second resolves by renumbering."""
    return bool(files) and all(f in VERSION_FILES or UPGRADE_FILE.match(f) for f in files)

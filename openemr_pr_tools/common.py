# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared helpers: which repository the tools work on, git and gh calls, open pull requests with
their files, in-memory merges, and the two project rules a profile can switch on: a version number
that every schema change takes (version claims) and counts that a file pins exactly (exact caps)."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

PRESETS = Path(__file__).resolve().parent / "presets"
PRESET_FOR_REPO = {"openemr/openemr": "openemr"}
CONFIG_NAME = ".pr-tools.json"

PRS_QUERY = """query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) { pullRequests(states: OPEN, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { number title isDraft author { login } headRefOid body files(first: 100) { totalCount nodes { path } } } } } }"""


class ToolError(RuntimeError):
    """A command the tool depends on failed, so its answer can't be trusted."""


@dataclass
class Target:
    """The checkout, the remote that points at the GitHub repository, that repository, its default
    branch, and the project profile."""
    repo: Path
    remote: str
    github: str
    base_branch: str
    config: dict = field(default_factory=dict)

    @property
    def base(self) -> str:
        return f"{self.remote}/{self.base_branch}"


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


def env(*names: str) -> str | None:
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    return None


def github_of(url: str) -> str | None:
    """owner/name from a GitHub remote URL, https or ssh."""
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="the git checkout (default: $PR_TOOLS_REPO, then the current directory's repository)")
    parser.add_argument("--remote", help="the remote that points at the GitHub repository (default: upstream if there is one, else origin)")
    parser.add_argument("--github-repo", help="owner/name on GitHub (default: read from the remote's URL)")
    parser.add_argument("--base-branch", help="the branch PRs merge into (default: the remote's default branch)")
    parser.add_argument("--config", help=f"project profile file (default: {CONFIG_NAME} in the checkout, then a built-in profile for the repository)")
    parser.add_argument("--preset", help="use a built-in profile by name, such as openemr")


def resolve(args: argparse.Namespace, need_checkout: bool = True) -> Target:
    """Works out the target from the arguments, the environment and the checkout."""
    repo_arg = getattr(args, "repo", None) or env("PR_TOOLS_REPO", "OPENEMR_REPO")
    if repo_arg:
        repo = Path(repo_arg).expanduser().resolve()
    else:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        repo = Path(top.stdout.strip()) if top.returncode == 0 else Path.cwd()
    remotes = git(repo, "remote").stdout.split()
    remote = getattr(args, "remote", None) or env("PR_TOOLS_REMOTE") or ("upstream" if "upstream" in remotes else "origin")
    github = getattr(args, "github_repo", None) or env("PR_TOOLS_GITHUB_REPO", "OPENEMR_GITHUB_REPO")
    if not github and remote in remotes:
        github = github_of(git(repo, "remote", "get-url", remote).stdout)
    if not github:
        raise ToolError(f"can't tell which GitHub repository {repo} is for: pass --github-repo owner/name")
    base_branch = getattr(args, "base_branch", None) or env("PR_TOOLS_BASE_BRANCH")
    if not base_branch and need_checkout and remote in remotes:
        symref = git(repo, "ls-remote", "--symref", remote, "HEAD").stdout
        match = re.search(r"^ref: refs/heads/(\S+)\s+HEAD", symref, re.M)
        base_branch = match.group(1) if match else None
    if not base_branch:
        base_branch = gh("repo", "view", github, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name").strip() or "main"
    return Target(repo, remote, github, base_branch, load_config(repo, github, getattr(args, "config", None), getattr(args, "preset", None)))


def load_config(repo: Path, github: str, path: str | None = None, preset: str | None = None) -> dict:
    """An explicit file, else .pr-tools.json in the checkout, else a built-in profile for the
    repository, else nothing: plain test merges, health, and the generic checks."""
    if path:
        return json.loads(Path(path).expanduser().read_text())
    if preset:
        return json.loads((PRESETS / f"{preset}.json").read_text())
    local = repo / CONFIG_NAME
    if local.exists():
        return json.loads(local.read_text())
    name = PRESET_FOR_REPO.get(github.lower())
    return json.loads((PRESETS / f"{name}.json").read_text()) if name else {}


def matches(path: str, patterns: list[str] | str | None) -> bool:
    if not patterns:
        return False
    return any(fnmatch.fnmatch(path, p) for p in ([patterns] if isinstance(patterns, str) else patterns))


def open_prs(github: str) -> list[dict]:
    """Every open PR with its complete list of changed files. GraphQL lists the first 100
    files, so larger PRs get theirs from the REST files endpoint."""
    owner, name = github.split("/")
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
            listing = gh("api", "--paginate", f"repos/{github}/pulls/{pr['number']}/files?per_page=100", "--jq", ".[].filename")
            pr["paths"] = set(listing.split())
    return prs


def fetch_prs(target: Target, numbers: list[int]) -> None:
    """Fetches the base branch and each PR's head as refs/remotes/<remote>/pr/<number>."""
    git(target.repo, "fetch", "-q", target.remote, target.base_branch, check=True)
    for i in range(0, len(numbers), 40):
        refs = [f"+pull/{n}/head:refs/remotes/{target.remote}/pr/{n}" for n in numbers[i:i + 40]]
        git(target.repo, "fetch", "-q", target.remote, *refs)


def merged(repo: Path, base: str, ref: str) -> tuple[str | None, list[str]]:
    """ref merged onto base in memory: the merge commit, or None and the conflicted files."""
    result = git(repo, "merge-tree", "--write-tree", "--name-only", base, ref)
    lines = result.stdout.strip().splitlines()
    if result.returncode == 1:
        return None, conflicted_files(lines[1:])
    if result.returncode != 0 or not lines:
        raise ToolError(f"merge-tree {ref}: {result.stderr.strip()[:200]}")
    commit = git(repo, "commit-tree", lines[0], "-p", base, "-p", ref, "-m", "pr-tools probe", check=True).stdout.strip()
    return commit, []


def conflicted_files(lines: list[str]) -> list[str]:
    """The file names `git merge-tree --name-only` lists before its messages."""
    return sorted({line for line in lines if line and not line.startswith(("Auto-merging", "CONFLICT"))})


def show(repo: Path, ref: str, path: str) -> str | None:
    result = git(repo, "show", f"{ref}:{path}")
    return result.stdout if result.returncode == 0 else None


def version_claim(repo: Path, ref: str, rule: dict | None) -> int | None:
    """The version number a tree claims, by the profile's version_claims rule."""
    if not rule:
        return None
    match = re.search(rule["pattern"], show(repo, ref, rule["file"]) or "")
    return int(match.group(1)) if match else None


def caps_mismatches(repo: Path, ref: str, rule: dict | None) -> list[str]:
    """Each exactly pinned count, by the profile's exact_caps rule, that its file doesn't hold.
    Two PRs that each lower the same count pass alone and fail together; comparing a merged tree
    with each PR alone shows that."""
    if not rule:
        return []
    source = show(repo, ref, rule["file"])
    if source is None:
        return []
    block = re.search(rule["block"], source, re.S)
    if block is None:
        raise ToolError(f"no pinned block in {rule['file']} at {ref}")
    bad = []
    for name, cap in re.findall(rule["entry"], block.group(1)):
        entries = (show(repo, ref, rule["counted"].format(name=name)) or "").count(rule["marker"])
        if entries != int(cap):
            bad.append(f"{name.rsplit('.', 1)[0]}: cap {cap}, entries {entries}")
    return bad


def is_version_conflict(files: list[str], rule: dict | None) -> bool:
    """Whether a conflict is only the version-number kind, which the PR that lands second resolves
    by taking the next number."""
    return bool(rule) and bool(files) and all(matches(f, rule.get("related", [rule["file"]])) for f in files)

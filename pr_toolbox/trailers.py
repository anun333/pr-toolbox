# SPDX-License-Identifier: GPL-3.0-or-later
"""pr-toolbox trailers: rewrite commit trailers on your own open PR branches, changing nothing else.

On every commit between the base branch and a PR's head, removes the trailer lines that match --remove, or swaps
them for another line with --replace. Each commit keeps its tree, author, committer and dates byte for byte; only
messages change, and the parents that follow from them. The result is checked before anything is pushed: the same
commits, each with its old tree, author and committer, and no matching trailer left. A dry run unless --push, which
force-pushes each branch with a lease on the head it read. Only PRs you opened are touched.

    pr-toolbox trailers [PR ...] [--mine] [--remove REGEX]... [--replace REGEX LINE]... [--push] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .common import ToolError, add_target_arguments, current_login, gh_json, git, github_of, resolve

TRAILER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*:\s")


@dataclass
class Rules:
    """Trailer lines to drop, and lines to drop with the line that takes their place."""
    remove: list[re.Pattern] = field(default_factory=list)
    replace: list[tuple[re.Pattern, str]] = field(default_factory=list)

    @classmethod
    def make(cls, remove: list[str], replace: list[tuple[str, str]]) -> "Rules":
        return cls([re.compile(p) for p in remove], [(re.compile(p), line) for p, line in replace])

    @classmethod
    def from_profile(cls, config: dict) -> "Rules":
        """The profile's "trailers" section: {"remove": [regex, ...], "replace": [{"from": regex, "to": line}, ...]}."""
        section = config.get("trailers") or {}
        return cls.make(section.get("remove") or [], [(r["from"], r["to"]) for r in section.get("replace") or []])

    def __bool__(self) -> bool:
        return bool(self.remove or self.replace)

    def hits(self, line: str) -> bool:
        if not TRAILER.match(line) or line in {to for _, to in self.replace}:
            return False
        return any(rx.search(line) for rx in self.remove) or any(rx.search(line) for rx, _ in self.replace)

    def patterns(self) -> list[str]:
        return [rx.pattern for rx in self.remove] + [rx.pattern for rx, _ in self.replace]


def rewrite_message(message: str, rules: Rules) -> tuple[str, list[str], list[str]]:
    """The message with matching trailer lines dropped and each replacement added once, plus the lines dropped and
    the lines added. Trailer-shaped lines ("Key: value") match anywhere in the message, since a squashed commit
    carries its parts' trailers in the middle. A message with nothing to drop comes back unchanged."""
    kept, removed, wanted = [], [], []
    for line in message.split("\n"):
        if rules.hits(line):
            removed.append(line)
            for rx, to in rules.replace:
                if rx.search(line) and to not in wanted:
                    wanted.append(to)
            continue
        kept.append(line)
    if not removed:
        return message, [], []
    body = "\n".join(kept).rstrip("\n")
    present = {line.strip().lower() for line in body.split("\n")}
    added = [line for line in wanted if line.strip().lower() not in present]
    if added:
        paragraphs = body.split("\n\n")
        block = len(paragraphs) > 1 and all(TRAILER.match(l) or l[:1] in " \t" for l in paragraphs[-1].split("\n") if l)
        body += ("\n" if block else "\n\n") + "\n".join(added)
    return body + "\n", removed, added


def _git_bytes(repo: Path, *args: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, input=input)
    if result.returncode != 0:
        raise ToolError(f"git {' '.join(args[:3])}: {result.stderr.decode(errors='replace').strip()[:300]}")
    return result.stdout


def rewrite_range(repo: Path, base: str, head: str, rules: Rules) -> tuple[dict[str, str], list[str], list[dict]]:
    """Writes a copy of every commit in base..head whose message or parents change. Returns old -> new for every
    commit, the commits in order, and the ones whose messages changed. Refuses signed commits (a rewrite drops the
    signature) and messages that aren't UTF-8."""
    shas = git(repo, "rev-list", "--topo-order", "--reverse", f"{base}..{head}", check=True).stdout.split()
    mapping, changed = {}, []
    for sha in shas:
        header, _, raw_message = _git_bytes(repo, "cat-file", "commit", sha).partition(b"\n\n")
        lines = header.split(b"\n")
        if any(line.startswith(b"gpgsig") for line in lines):
            raise ToolError(f"{sha[:10]} is signed, and rewriting it would drop the signature")
        encoding = next((line[9:].strip().lower() for line in lines if line.startswith(b"encoding ")), b"utf-8")
        try:
            if encoding not in (b"utf-8", b"utf8"):
                raise UnicodeError
            message = raw_message.decode("utf-8")
        except UnicodeError:
            raise ToolError(f"{sha[:10]}'s message isn't UTF-8") from None
        new_message, removed, added = rewrite_message(message, rules)
        parents = [line[7:].decode() for line in lines if line.startswith(b"parent ")]
        new_parents = iter(mapping.get(p, p) for p in parents)
        if new_message == message and all(mapping.get(p, p) == p for p in parents):
            mapping[sha] = sha
            continue
        out = [b"parent " + next(new_parents).encode() if line.startswith(b"parent ") else line for line in lines]
        obj = b"\n".join(out) + b"\n\n" + new_message.encode("utf-8")
        mapping[sha] = _git_bytes(repo, "hash-object", "-t", "commit", "-w", "--stdin", input=obj).decode().strip()
        if removed:
            changed.append({"sha": sha, "new": mapping[sha], "subject": message.split("\n", 1)[0],
                            "removed": removed, "added": added})
    return mapping, shas, changed


def verify(repo: Path, base: str, mapping: dict[str, str], shas: list[str], rules: Rules) -> list[str]:
    """What is wrong with a rewrite, if anything: every commit must keep its tree, author and committer, the new
    range must be exactly the new commits, and no matching trailer may be left."""
    if not shas:
        return []
    fmt = "--format=%H%x00%T%x00%an <%ae> %ad%x00%cn <%ce> %cd"

    def facts(commits: list[str]) -> dict[str, list[str]]:
        out = git(repo, "log", "--no-walk=unsorted", "--date=raw", fmt, *commits, check=True).stdout
        return {row.split("\x00")[0]: row.split("\x00")[1:] for row in out.splitlines() if row}

    old, new = facts(shas), facts([mapping[s] for s in shas])
    problems = [f"{s[:10]}: tree, author or committer changed" for s in shas if old[s] != new[mapping[s]]]
    new_head = mapping[shas[-1]] if shas[-1] in mapping else shas[-1]
    listed = set(git(repo, "rev-list", f"{base}..{new_head}", check=True).stdout.split())
    if listed != {mapping[s] for s in shas}:
        problems.append(f"the new branch has {len(listed)} commits where the old had {len(shas)}")
    log = git(repo, "log", "--format=%H%x00%B%x1e", f"{base}..{new_head}", check=True).stdout
    for entry in filter(str.strip, log.split("\x1e")):
        sha, message = entry.strip().split("\x00", 1)
        if any(rules.hits(line) for line in message.split("\n")):
            problems.append(f"{sha[:10]} still has a matching trailer")
    return problems


def remote_for(repo: Path, github: str) -> str | None:
    """The remote whose push URL points at a GitHub repository."""
    for name in git(repo, "remote").stdout.split():
        url = git(repo, "remote", "get-url", "--push", name).stdout
        if (github_of(url) or "").lower() == github.lower():
            return name
    return None


def push(repo: Path, remote: str, branch: str, old: str, new: str) -> None:
    """Force-pushes new to the branch only if the branch is still at old."""
    git(repo, "push", "-q", f"--force-with-lease=refs/heads/{branch}:{old}", remote, f"{new}:refs/heads/{branch}", check=True)


def pr_info(github: str, number: int) -> dict:
    return gh_json("api", f"repos/{github}/pulls/{number}")


def my_open_prs(github: str) -> list[int]:
    listing = gh_json("pr", "list", "--repo", github, "--author", "@me", "--state", "open", "--json", "number", "--limit", "500")
    return sorted(pr["number"] for pr in listing)


@dataclass
class Result:
    number: int
    branch: str = ""
    head_repo: str = ""
    head: str = ""
    new: str = ""
    commits: int = 0
    changed: list[dict] = field(default_factory=list)
    status: str = ""           # rewritten, pushed, unchanged, skipped, refused
    detail: str = ""


def run_one(target, number: int, login: str, rules: Rules, args: argparse.Namespace) -> Result:
    result = Result(number)
    info = pr_info(target.github, number)
    if info.get("state") != "open":
        return _set(result, "skipped", "not open")
    if (info.get("user") or {}).get("login", "").lower() != login.lower():
        return _set(result, "skipped", f"opened by {(info.get('user') or {}).get('login')}, not you")
    head_info = info.get("head") or {}
    result.branch, result.head = head_info.get("ref", ""), head_info.get("sha", "")
    result.head_repo = (head_info.get("repo") or {}).get("full_name", "")
    if not result.head_repo:
        return _set(result, "skipped", "its branch's repository no longer exists")
    ref = f"refs/remotes/{target.remote}/pr/{number}"
    git(target.repo, "fetch", "-q", target.remote, f"+pull/{number}/head:{ref}", check=True)
    fetched = git(target.repo, "rev-parse", ref, check=True).stdout.strip()
    if fetched != result.head:
        return _set(result, "refused", f"fetched {fetched[:10]} but GitHub says the head is {result.head[:10]}; run again")
    base = git(target.repo, "merge-base", target.base, fetched, check=True).stdout.strip()
    try:
        mapping, shas, result.changed = rewrite_range(target.repo, base, fetched, rules)
    except ToolError as error:
        return _set(result, "refused", str(error))
    result.commits, result.new = len(shas), mapping.get(fetched, fetched)
    if result.new == fetched:
        return _set(result, "unchanged", "no matching trailers")
    problems = verify(target.repo, base, mapping, shas, rules)
    if problems:
        return _set(result, "refused", "; ".join(problems))
    checked = f"checked: {_n(len(shas), 'commit')}, each with its old tree, author and committer; no matching trailer left"
    if not args.push:
        return _set(result, "rewritten", checked + "; not pushed (dry run)")
    remote = args.push_remote or remote_for(target.repo, result.head_repo)
    if not remote:
        return _set(result, "refused", f"no remote pushes to {result.head_repo}; add one, or pass --push-remote")
    try:
        push(target.repo, remote, result.branch, fetched, result.new)
    except ToolError as error:
        return _set(result, "refused", f"push refused, nothing changed: {error}")
    git(target.repo, "update-ref", ref, result.new)
    note = checked + f"; pushed to {remote}/{result.branch}"
    local = git(target.repo, "rev-parse", "--verify", "-q", f"refs/heads/{result.branch}").stdout.strip()
    if local == fetched:
        note += f"; your local branch {result.branch} still points at the old head (git branch -f, or git reset --keep {result.new[:10]} if checked out)"
    return _set(result, "pushed", note)


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _set(result: Result, status: str, detail: str) -> Result:
    result.status, result.detail = status, detail
    return result


def report(results: list[Result], push_requested: bool) -> str:
    lines = []
    for r in results:
        where = f" {r.branch} ({r.head_repo})" if r.branch else ""
        if r.status in {"rewritten", "pushed"}:
            lines.append(f"#{r.number}{where}: {len(r.changed)} of {_n(r.commits, 'commit')} rewritten, {r.head[:10]} -> {r.new[:10]}")
            for c in r.changed:
                lines.append(f"  {c['sha'][:10]} {c['subject'][:70]}")
                lines += [f"     - {line}" for line in c["removed"]] + [f"     + {line}" for line in c["added"]]
            lines.append(f"  {r.detail}")
        else:
            lines.append(f"#{r.number}{where}: {r.status}, {r.detail}")
    pending = sum(r.status == "rewritten" for r in results)
    if pending and not push_requested:
        lines.append(f"\nDry run: nothing pushed. Run again with --push to force-push {pending} branch"
                     f"{'es' if pending > 1 else ''}, each with a lease on the head shown.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pr-toolbox trailers", description=__doc__.split("\n")[0])
    add_target_arguments(parser)
    parser.add_argument("numbers", nargs="*", type=int, metavar="PR", help="your open PRs")
    parser.add_argument("--mine", action="store_true", help="every open PR you opened on the repository")
    parser.add_argument("--remove", action="append", default=[], metavar="REGEX", help="drop trailer lines matching REGEX (repeatable)")
    parser.add_argument("--replace", action="append", nargs=2, default=[], metavar=("REGEX", "LINE"),
                        help="drop trailer lines matching REGEX, and add LINE once where any were dropped (repeatable)")
    parser.add_argument("--push", action="store_true", help="force-push each rewritten branch, with a lease on the head that was read")
    parser.add_argument("--push-remote", help="the remote to push to (default: the one whose URL is the PR's head repository)")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args(argv)
    target = resolve(args)
    rules = Rules.make(args.remove, [tuple(r) for r in args.replace]) if args.remove or args.replace else Rules.from_profile(target.config)
    if not rules:
        print('no trailer rules: pass --remove or --replace, or add a "trailers" section to the profile', file=sys.stderr)
        return 2
    numbers = sorted(set(args.numbers) | set(my_open_prs(target.github) if args.mine else []))
    if not numbers:
        print("name the PRs, or pass --mine", file=sys.stderr)
        return 2
    login = current_login()
    git(target.repo, "fetch", "-q", target.remote, target.base_branch, check=True)
    results = [run_one(target, n, login, rules, args) for n in numbers]
    print(json.dumps([asdict(r) for r in results], indent=1) if args.json else report(results, args.push))
    return 1 if any(r.status == "refused" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())

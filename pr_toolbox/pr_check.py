# SPDX-License-Identifier: GPL-3.0-or-later
"""Pre-push check: what a project's CI will say about a branch, before it says it.

Built-in steps work on any repository: the branch merges onto the base branch cleanly, codespell on
the changed files, a scan of the diff for secret values, commit trailers, and the project profile's
exact caps and version-number rules when it defines them. The profile's check_steps add the
project's own commands (linters, static analysis, the changed tests), run on the host or in a
container, once with all the matching changed files or once per file.

It reads the checkout and GitHub and runs commands. It never commits, pushes or comments.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .common import (Target, add_target_arguments, caps_mismatches, gh, git, matches, merged, open_prs, resolve, version_claim)
from .trailers import Rules

BUILT_IN_FIRST = ["merges"]
BUILT_IN_LAST = ["codespell", "caps", "version", "version-claims", "secrets", "trailers"]


def find_container(repo: Path, app_dir: str) -> str | None:
    """The running container that mounts this checkout at app_dir, as openemr-cmd's stacks do."""
    names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
    for name in names:
        mounts = subprocess.run(["docker", "inspect", "-f", "{{range .Mounts}}{{.Source}}|{{.Destination}}\n{{end}}", name],
                                capture_output=True, text=True).stdout.splitlines()
        if f"{repo}|{app_dir}" in mounts:
            return name
    return None


def quoted(files: list[str]) -> str:
    return " ".join("'" + f.replace("'", "'\\''") + "'" for f in files)


def tail(text: str, lines: int) -> str:
    return " | ".join(text.strip().splitlines()[-lines:])


class Check:
    def __init__(self, args: argparse.Namespace, target: Target):
        self.args, self.target, self.config = args, target, target.config
        self.repo = target.repo
        self.base = target.base
        self.merge_base = git(self.repo, "merge-base", self.base, "HEAD", check=True).stdout.strip()
        tracked = git(self.repo, "diff", "--name-only", self.merge_base).stdout.split()
        untracked = git(self.repo, "ls-files", "--others", "--exclude-standard").stdout.split()
        self.changed = sorted({f for f in tracked + untracked if (self.repo / f).exists()})
        self.app_dir = args.app_dir or (self.config.get("container") or {}).get("app_dir") or str(self.repo)
        self._container: str | None = None if args.container in (None, "auto", "none") else args.container

    @property
    def container(self) -> str:
        if self._container is None:
            if self.args.container == "none":
                raise RuntimeError("this step runs in a container, and --container none was given")
            self._container = find_container(self.repo, self.app_dir)
            if self._container is None:
                raise RuntimeError(f"no running container mounts {self.repo} at {self.app_dir}; start it or pass --container NAME")
        return self._container

    def shell(self, step: dict, command: str, timeout: int) -> subprocess.CompletedProcess:
        if step.get("where") == "container":
            cmd = ["docker", "exec", "-w", self.app_dir, self.container, "sh", "-c", command]
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return subprocess.run(["sh", "-c", command], cwd=self.repo, capture_output=True, text=True, timeout=timeout)

    def fill(self, text: str, files: list[str] | None = None, file: str | None = None) -> str:
        values = {"app_dir": self.app_dir, "workers": str(self.args.workers), "base": self.base,
                  "files": quoted(files or []), "file": quoted([file]) if file else ""}
        return re.sub(r"\{(app_dir|workers|base|files|file)\}", lambda m: values[m.group(1)], text)

    def command_step(self, step: dict) -> tuple[str, str]:
        pattern = step.get("each") or step.get("files")
        files = [f for f in self.changed if (not pattern or matches(f, pattern)) and not matches(f, step.get("exclude"))]
        if not files and not step.get("always"):
            return "skip", f"no changed files match {pattern}"
        missing = [n for n in ([step["needs"]] if isinstance(step.get("needs"), str) else step.get("needs") or []) if not (self.repo / n).exists()]
        if missing:
            return "skip", f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'}n't in the checkout" + (f": {step['needs_hint']}" if step.get("needs_hint") else "")
        if step.get("write"):
            content = self.fill(step["write"]["content"])
            path = step["write"]["path"]
            if step.get("where") == "container":
                done = subprocess.run(["docker", "exec", "-i", self.container, "sh", "-c", f"cat > {quoted([path])}"],
                                      input=content, capture_output=True, text=True)
                if done.returncode != 0:
                    return "fail", f"couldn't write {path}: {done.stderr.strip()[:200]}"
            else:
                Path(path).write_text(content)
        timeout = int(step.get("timeout", 1800))
        fail_pattern = re.compile(step["fail_pattern"]) if step.get("fail_pattern") else None

        def passed(result: subprocess.CompletedProcess) -> bool:
            return result.returncode == 0 and not (fail_pattern and fail_pattern.search(result.stdout + result.stderr))

        if step.get("each"):
            results, failed = [], False
            for f in files:
                result = self.shell(step, self.fill(step["run"], file=f), timeout)
                ok = passed(result)
                failed |= not ok
                last = ((result.stdout + result.stderr).strip().splitlines() or ["(no output)"])[-1]
                results.append(f"{f}: {last}" if not ok or step.get("verbose") else f"{f}: ok" if len(files) <= 3 else None)
            shown = [r for r in results if r]
            summary = "; ".join(shown) if shown else f"{len(files)} files"
            return ("fail" if failed else "pass"), summary
        result = self.shell(step, self.fill(step["run"], files=files), timeout)
        if passed(result):
            return "pass", "done (runs whatever changed)" if step.get("always") else f"{len(files)} files"
        return "fail", tail(result.stdout + result.stderr, int(step.get("tail", 15)))

    def merges(self) -> tuple[str, str]:
        commit, conflicts = merged(self.repo, self.base, "HEAD")
        return ("fail", f"conflicts with {self.base}: " + ", ".join(conflicts)) if commit is None else ("pass", f"merges onto {self.base}")

    def codespell(self) -> tuple[str, str]:
        if not shutil.which("codespell"):
            return "skip", "codespell isn't installed on this machine"
        files = [f for f in self.changed if not matches(f, self.config.get("codespell_exclude"))]
        if not files:
            return "skip", "nothing to read"
        result = subprocess.run(["codespell", *files], cwd=self.repo, capture_output=True, text=True)
        return ("pass", f"{len(files)} files") if result.returncode == 0 else ("fail", tail(result.stdout, 15))

    def caps(self) -> tuple[str, str]:
        rule = self.config.get("exact_caps")
        if not rule:
            return "skip", "the profile pins no counts"
        commit, conflicts = merged(self.repo, self.base, "HEAD")
        if commit is None:
            return "fail", "the branch conflicts with the base: " + ", ".join(conflicts)
        new = sorted(set(caps_mismatches(self.repo, commit, rule)) - set(caps_mismatches(self.repo, self.base, rule)))
        note = " (committed work only)" if git(self.repo, "status", "--porcelain").stdout.strip() else ""
        return ("fail", "; ".join(new) + note) if new else ("pass", "no pinned count off" + note)

    def working_claim(self, rule: dict) -> int | None:
        path = self.repo / rule["file"]
        match = re.search(rule["pattern"], path.read_text(errors="replace")) if path.exists() else None
        return int(match.group(1)) if match else None

    def version(self) -> tuple[str, str]:
        rule = self.config.get("version_claims")
        if not rule:
            return "skip", "the profile has no version rule"
        if not any(matches(f, rule.get("related", [rule["file"]]) + rule.get("bump_when", [])) for f in self.changed):
            return "skip", "no versioned files changed"
        problems = []
        blocks = rule.get("blocks")
        for f in (f for f in self.changed if blocks and matches(f, blocks["files"])):
            text = (self.repo / f).read_text(errors="replace")
            opens, closes = len(re.findall(blocks["open"], text, re.M)), len(re.findall(blocks["close"], text, re.M))
            if opens != closes:
                problems.append(f"{f}: {opens} blocks opened, {closes} closed")
        mine, theirs = self.working_claim(rule), version_claim(self.repo, self.base, rule)
        noun = rule.get("noun", "the version")
        if any(matches(f, rule.get("bump_when", [])) for f in self.changed) and (mine is None or theirs is None or mine <= theirs):
            problems.append(f"{noun} is {mine}, not above the base's {theirs}")
        for mirror in rule.get("mirrors", []):
            path = self.repo / mirror["file"]
            found = re.search(mirror["pattern"], path.read_text(errors="replace")) if path.exists() else None
            if found and mine is not None and int(found.group(1)) != mine:
                problems.append(f"{rule['file']} says {mine}, {mirror['file']} says {found.group(1)}")
        return ("fail", "; ".join(problems)) if problems else ("pass", f"{noun} {mine} (base {theirs}), blocks balanced")

    def version_claims(self) -> tuple[str, str]:
        rule = self.config.get("version_claims")
        if not rule:
            return "skip", "the profile has no version rule"
        if rule["file"] not in self.changed:
            return "skip", f"{rule['file']} unchanged"
        mine = self.working_claim(rule)
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        clashes = []
        for pr in open_prs(self.target.github):
            if rule["file"] not in pr["paths"] or pr["headRefOid"] == head:
                continue
            text = gh("api", f"repos/{self.target.github}/contents/{rule['file']}?ref={pr['headRefOid']}", "-H", "Accept: application/vnd.github.raw")
            claim = re.search(rule["pattern"], text)
            if claim and int(claim.group(1)) == mine:
                clashes.append(f"#{pr['number']} ({pr['author']})")
        noun = rule.get("noun", "version")
        return ("fail", f"{noun} {mine} is also claimed by " + ", ".join(clashes)) if clashes else ("pass", f"no other open PR claims {mine}")

    def secrets(self) -> tuple[str, str]:
        if not self.args.secrets_env:
            return "skip", "no --secrets-env given"
        public = {k.strip() for k in (self.args.public_keys or "").split(",") if k.strip()}
        values = {}
        for line in Path(self.args.secrets_env).expanduser().read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                value = value.strip().strip("'\"")
                if key not in public and len(value) >= 6:
                    values[key] = value
        diff = git(self.repo, "diff", self.merge_base).stdout
        for f in git(self.repo, "ls-files", "--others", "--exclude-standard").stdout.split():
            try:
                diff += (self.repo / f).read_text(errors="replace")
            except OSError:
                pass
        hits = sorted(k for k, v in values.items() if v in diff)
        return ("fail", "values of " + ", ".join(hits) + " appear in the diff") if hits else ("pass", f"{len(values)} values checked")

    def trailers(self) -> tuple[str, str]:
        # without flags, the trailers the profile's "trailers" section removes or replaces are the forbidden ones
        profile_rules = Rules.from_profile(self.config)
        forbid = self.args.forbid_trailer or profile_rules.patterns()
        if not forbid and not self.args.require_trailer:
            return "skip", "no trailer rules given"
        log = git(self.repo, "log", "--format=%h%x00%B%x1e", f"{self.merge_base}..HEAD").stdout
        problems = []
        for entry in filter(str.strip, log.split("\x1e")):
            sha, message = entry.strip().split("\x00", 1)
            for pattern in forbid:
                if re.search(pattern, message, re.M):
                    problems.append(f"{sha} has /{pattern}/")
            for pattern in self.args.require_trailer or []:
                if not re.search(pattern, message, re.M):
                    problems.append(f"{sha} lacks /{pattern}/")
        if problems and not self.args.forbid_trailer and profile_rules:
            problems.append("pr-toolbox trailers rewrites them once the branch is a PR")
        return ("fail", "; ".join(problems)) if problems else ("pass", "commits follow the trailer rules")

    def plan(self) -> list[tuple[str, object]]:
        built_in = {"merges": self.merges, "codespell": self.codespell, "caps": self.caps, "version": self.version,
                    "version-claims": self.version_claims, "secrets": self.secrets, "trailers": self.trailers}
        steps = [(name, built_in[name]) for name in BUILT_IN_FIRST]
        steps += [(step["name"], (lambda s=step: self.command_step(s))) for step in self.config.get("check_steps", [])]
        steps += [(name, built_in[name]) for name in BUILT_IN_LAST]
        return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pr-toolbox check", description=__doc__.split("\n")[0])
    add_target_arguments(parser)
    parser.add_argument("--worktree", help="the same as --repo")
    parser.add_argument("--container", default="auto", help="container for the profile's container steps: auto (the one that mounts the checkout), a name, or none")
    parser.add_argument("--app-dir", help="the checkout's path inside the container (default: the profile's, else the checkout's own path)")
    parser.add_argument("--workers", type=int, default=3, help="{workers} in the profile's commands, such as PHPStan's processes (default: 3)")
    parser.add_argument("--secrets-env", help="KEY=VALUE file whose values must not appear in the diff")
    parser.add_argument("--public-keys", help="comma-separated keys in that file whose values are public")
    parser.add_argument("--forbid-trailer", action="append", help="regex no commit message may match (repeatable)")
    parser.add_argument("--require-trailer", action="append", help="regex every commit message must match (repeatable)")
    parser.add_argument("--skip", default="", help="comma-separated step names to skip")
    parser.add_argument("--list", action="store_true", help="list the steps the profile gives and exit")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args(argv)
    if args.worktree and not args.repo:
        args.repo = args.worktree
    target = resolve(args)
    check = Check(args, target)
    plan = check.plan()
    if args.list:
        print(f"{target.github} ({target.config.get('name', 'no profile')}): " + ", ".join(name for name, _ in plan))
        return 0
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    results = []
    for name, run in plan:
        if name in skip:
            results.append({"step": name, "status": "skip", "detail": "skipped by --skip"})
            continue
        try:
            status, detail = run()
        except Exception as error:  # a step that can't run is a failure, not a pass
            status, detail = "fail", f"{type(error).__name__}: {error}"
        results.append({"step": name, "status": status, "detail": detail})
        if not args.json:
            print(f"{status.upper():5} {name:14} {detail}", flush=True)
    failed = [r["step"] for r in results if r["status"] == "fail"]
    if args.json:
        print(json.dumps({"github": target.github, "base": target.base, "changed": check.changed, "results": results, "failed": failed}, indent=1))
    else:
        print(f"\n{len(check.changed)} files changed against {target.base}; " + (f"FAILED: {', '.join(failed)}" if failed else "all checks passed or skipped"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

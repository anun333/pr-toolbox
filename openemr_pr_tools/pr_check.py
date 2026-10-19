# SPDX-License-Identifier: GPL-3.0-or-later
"""Pre-push check: what OpenEMR's CI will say about a branch, before it says it.

Run from an OpenEMR worktree whose dev stack (openemr-cmd's easy or easy-light) is up. In order:
syntax, phpcs, rector and codespell on the changed files; the full PHPStan analysis with CI's
configuration (with a capped number of workers); the changed test files; PHPStan's exact baseline
caps on the branch merged onto master; the upgrade-script and v_database rules, including whether
another open PR claims the same number; a scan of the diff for secret values; and commit trailers.

It reads the worktree and GitHub and runs tools in the container. It never commits, pushes or
comments.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .common import GITHUB_REPO, UPGRADE_FILE, caps_mismatches, gh, git, merged, open_prs, v_database

STEPS = ["php-lint", "phpcs", "rector", "codespell", "phpstan", "tests", "caps", "upgrade-sql", "v-database", "secrets", "trailers"]


def find_container(worktree: Path, app_dir: str) -> str:
    """The running container that mounts this worktree at app_dir, as openemr-cmd's stacks do."""
    names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
    for name in names:
        mounts = subprocess.run(["docker", "inspect", "-f", "{{range .Mounts}}{{.Source}}|{{.Destination}}\n{{end}}", name],
                                capture_output=True, text=True).stdout.splitlines()
        if f"{worktree}|{app_dir}" in mounts:
            return name
    raise RuntimeError(f"no running container mounts {worktree} at {app_dir}; start the stack or pass --container")


class Check:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.repo = Path(args.worktree).expanduser().resolve()
        if args.container == "auto":
            args.container = find_container(self.repo, args.app_dir)
        self.base = args.base
        self.merge_base = git(self.repo, "merge-base", self.base, "HEAD", check=True).stdout.strip()
        tracked = git(self.repo, "diff", "--name-only", self.merge_base).stdout.split()
        untracked = git(self.repo, "ls-files", "--others", "--exclude-standard").stdout.split()
        self.changed = sorted({f for f in tracked + untracked if (self.repo / f).exists()})
        self.deleted = sorted(set(tracked) - set(self.changed))
        self.php = [f for f in self.changed if f.endswith(".php")]

    def container(self, command: str, timeout: int = 1800) -> subprocess.CompletedProcess:
        cmd = ["docker", "exec", "-w", self.args.app_dir, self.args.container, "sh", "-c", command]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    @staticmethod
    def quoted(files: list[str]) -> str:
        return " ".join("'" + f.replace("'", "'\\''") + "'" for f in files)

    def php_lint(self) -> tuple[str, str]:
        if not self.php:
            return "skip", "no PHP files changed"
        bad = []
        for f in self.php:
            r = self.container(f"php -l {self.quoted([f])}")
            if r.returncode != 0:
                bad.append(f"{f}: {(r.stdout + r.stderr).strip().splitlines()[-1]}")
        return ("fail", "; ".join(bad)) if bad else ("pass", f"{len(self.php)} files")

    def phpcs(self) -> tuple[str, str]:
        if not self.php:
            return "skip", "no PHP files changed"
        r = self.container(f"vendor/bin/phpcs --standard=phpcs.xml.dist -q --report=summary {self.quoted(self.php)}")
        return ("pass", f"{len(self.php)} files") if r.returncode == 0 else ("fail", tail(r.stdout, 15))

    def rector(self) -> tuple[str, str]:
        if not self.php:
            return "skip", "no PHP files changed"
        r = self.container(f"vendor/bin/rector process --dry-run --no-progress-bar {self.quoted(self.php)}")
        out = r.stdout + r.stderr
        if re.search(r"would have (been )?changed|\d+ files? with changes", out):
            return "fail", tail("\n".join(l for l in out.splitlines() if l.strip().startswith(("-", "+", "1)", "2)", "3)")) or out), 15)
        return ("pass", "no changes") if "[OK]" in out else ("fail", tail(out, 10))

    def codespell(self) -> tuple[str, str]:
        if not shutil.which("codespell"):
            return "skip", "codespell isn't installed on this machine"
        files = [f for f in self.changed if not f.startswith(".phpstan/")]
        if not files:
            return "skip", "nothing to read"
        r = subprocess.run(["codespell", *files], cwd=self.repo, capture_output=True, text=True)
        return ("pass", f"{len(files)} files") if r.returncode == 0 else ("fail", tail(r.stdout, 15))

    def phpstan(self) -> tuple[str, str]:
        neon = (f"includes:\n  - {self.args.app_dir}/.phpstan/phpstan.ci.neon\n"
                f"parameters:\n  parallel:\n    maximumNumberOfProcesses: {self.args.workers}\n")
        write = subprocess.run(["docker", "exec", "-i", self.args.container, "sh", "-c", "cat > /tmp/openemr-pr-tools-phpstan.neon"],
                               input=neon, capture_output=True, text=True)
        if write.returncode != 0:
            return "fail", f"couldn't write the PHPStan config: {write.stderr.strip()[:200]}"
        r = self.container("php -d memory_limit=8G vendor/bin/phpstan --memory-limit=8G analyze -c /tmp/openemr-pr-tools-phpstan.neon "
                           "--error-format=raw --no-progress", timeout=3600)
        if r.returncode == 0:
            return "pass", f"full analysis, CI configuration, {self.args.workers} workers"
        return "fail", tail(r.stdout + r.stderr, 20)

    def tests(self) -> tuple[str, str]:
        files = [f for f in self.changed if f.startswith("tests/Tests/") and f.endswith("Test.php")]
        if not files:
            return "skip", "no test files changed"
        results, failed = [], False
        for f in files:
            config = "phpunit-isolated.xml" if f.startswith("tests/Tests/Isolated/") else "phpunit.xml"
            r = self.container(f"vendor/bin/phpunit -c {config} {self.quoted([f])}", timeout=3600)
            last = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
            failed |= r.returncode != 0
            results.append(f"{f}: {last}")
        return ("fail" if failed else "pass"), "; ".join(results)

    def caps(self) -> tuple[str, str]:
        commit, conflicts = merged(self.repo, self.base, "HEAD")
        if commit is None:
            return "fail", "the branch conflicts with master: " + ", ".join(conflicts)
        before = set(caps_mismatches(self.repo, self.base))
        after = set(caps_mismatches(self.repo, commit))
        new = sorted(after - before)
        note = " (committed work only)" if git(self.repo, "status", "--porcelain").stdout.strip() else ""
        return ("fail", "; ".join(new) + note) if new else ("pass", "no exact cap off" + note)

    def upgrade_sql(self) -> tuple[str, str]:
        sql = [f for f in self.changed if f.startswith("sql/")]
        if not sql:
            return "skip", "no SQL changed"
        problems = []
        for f in sql:
            if UPGRADE_FILE.match(f):
                text = (self.repo / f).read_text()
                opens = len(re.findall(r"^#If", text, re.M))
                ends = len(re.findall(r"^#EndIf", text, re.M))
                if opens != ends:
                    problems.append(f"{f}: {opens} #If blocks, {ends} #EndIf")
        mine, theirs = working_v_database(self.repo), v_database(self.repo, self.base)
        header = re.search(r"-- v_database: (\d+)", (self.repo / "sql/database.sql").read_text(errors="replace")) if (self.repo / "sql/database.sql").exists() else None
        if mine is None or theirs is None or mine <= theirs:
            problems.append(f"SQL changed but v_database is {mine}, not above master's {theirs}")
        if header and mine is not None and int(header.group(1)) != mine:
            problems.append(f"version.php says {mine}, database.sql's header {header.group(1)}")
        return ("fail", "; ".join(problems)) if problems else ("pass", f"v_database {mine} (master {theirs}), upgrade blocks balanced")

    def v_database_claims(self) -> tuple[str, str]:
        if "version.php" not in self.changed:
            return "skip", "version.php unchanged"
        mine = working_v_database(self.repo)
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        clashes = []
        for pr in open_prs():
            if "version.php" not in pr["paths"] or pr["headRefOid"] == head:
                continue
            text = gh("api", f"repos/{GITHUB_REPO}/contents/version.php?ref={pr['headRefOid']}", "-H", "Accept: application/vnd.github.raw")
            claim = re.search(r"\$v_database = (\d+);", text)
            if claim and int(claim.group(1)) == mine:
                clashes.append(f"#{pr['number']} ({pr['author']})")
        return ("fail", f"v_database {mine} is also claimed by " + ", ".join(clashes)) if clashes else ("pass", f"no other open PR claims {mine}")

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
        if not self.args.forbid_trailer and not self.args.require_trailer:
            return "skip", "no trailer rules given"
        log = git(self.repo, "log", "--format=%h%x00%B%x1e", f"{self.merge_base}..HEAD").stdout
        problems = []
        for entry in filter(str.strip, log.split("\x1e")):
            sha, message = entry.strip().split("\x00", 1)
            for pattern in self.args.forbid_trailer or []:
                if re.search(pattern, message, re.M):
                    problems.append(f"{sha} has /{pattern}/")
            for pattern in self.args.require_trailer or []:
                if not re.search(pattern, message, re.M):
                    problems.append(f"{sha} lacks /{pattern}/")
        return ("fail", "; ".join(problems)) if problems else ("pass", "commits follow the trailer rules")


def working_v_database(repo: Path) -> int | None:
    match = re.search(r"\$v_database = (\d+);", (repo / "version.php").read_text()) if (repo / "version.php").exists() else None
    return int(match.group(1)) if match else None


def tail(text: str, lines: int) -> str:
    return " | ".join(text.strip().splitlines()[-lines:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openemr-pr-tools check", description=__doc__.split("\n")[0])
    parser.add_argument("--worktree", default=".", help="the OpenEMR worktree to check (default: .)")
    parser.add_argument("--container", default="auto", help="the stack's OpenEMR container (default: the one that mounts the worktree)")
    parser.add_argument("--app-dir", default="/var/www/localhost/htdocs/openemr", help="the worktree's path inside the container")
    parser.add_argument("--base", default="upstream/master", help="what the branch is compared with (default: upstream/master)")
    parser.add_argument("--workers", type=int, default=3, help="PHPStan's parallel workers (default: 3)")
    parser.add_argument("--secrets-env", help="KEY=VALUE file whose values must not appear in the diff")
    parser.add_argument("--public-keys", help="comma-separated keys in that file whose values are public")
    parser.add_argument("--forbid-trailer", action="append", help="regex no commit message may match (repeatable)")
    parser.add_argument("--require-trailer", action="append", help="regex every commit message must match (repeatable)")
    parser.add_argument("--skip", default="", help=f"comma-separated steps to skip: {', '.join(STEPS)}")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args(argv)
    check = Check(args)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    runners = {"php-lint": check.php_lint, "phpcs": check.phpcs, "rector": check.rector, "codespell": check.codespell,
               "phpstan": check.phpstan, "tests": check.tests, "caps": check.caps, "upgrade-sql": check.upgrade_sql,
               "v-database": check.v_database_claims, "secrets": check.secrets, "trailers": check.trailers}
    results = []
    for step in STEPS:
        if step in skip:
            results.append({"step": step, "status": "skip", "detail": "skipped by --skip"})
            continue
        try:
            status, detail = runners[step]()
        except Exception as error:  # a step that can't run is a failure, not a pass
            status, detail = "fail", f"{type(error).__name__}: {error}"
        results.append({"step": step, "status": status, "detail": detail})
        if not args.json:
            print(f"{status.upper():5} {step:12} {detail}", flush=True)
    failed = [r["step"] for r in results if r["status"] == "fail"]
    if args.json:
        print(json.dumps({"changed": check.changed, "results": results, "failed": failed}, indent=1))
    else:
        print(f"\n{len(check.changed)} files changed against {args.base}; " + (f"FAILED: {', '.join(failed)}" if failed else "all checks passed or skipped"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

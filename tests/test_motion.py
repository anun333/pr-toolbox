# SPDX-License-Identifier: GPL-3.0-or-later
"""pr-toolbox motion against a throwaway repository with crafted pull-request refs: every check must find the case
built for it, a wrong control must stop the build, an incremental rebuild after the base moves must equal a full
one, and a quote that no longer matches its source must stop the page. No network, no OpenEMR checkout."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOTION = ROOT / "pr_toolbox" / "motion"
sys.path.insert(0, str(ROOT))

from pr_toolbox.motion import cli  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def write(repo: Path, files: dict[str, str | None]) -> None:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def pr(repo: Path, n: int, base: str, files: dict[str, str | None]) -> str:
    """A pull request: a commit on top of base, kept as refs/pr/<n>."""
    git(repo, "checkout", "-q", "--detach", base)
    sha = commit(repo, files, f"PR {n}")
    git(repo, "update-ref", f"refs/pr/{n}", sha)
    git(repo, "checkout", "-q", "master")
    return sha


PROFILE = {
    "github": "example/app", "owner": "example", "name": "app", "remote": "origin", "base_branch": "master",
    "version_claims": {"file": "version.txt", "pattern": r"db = (\d+)", "related": ["version.txt"]},
    "exact_caps": None,
    **cli.DEFAULTS,
    "title": "App Work in Motion", "project": "App", "mechanical": r"^version\.txt$", "symbols": None,
    "release_tags": {"pattern": r"^v(\d+)\.(\d+)\.0$", "count": 2},
    "name_paths": ["routes"],
    "names": [{"kind": "route", "where": {"prefix": "routes/"}, "pattern": r"""['"]((?:GET|POST) /api/[^'"]*)['"]"""}],
    "streams": {"general": [], "rules_package": None, "streams": []},
}


class MotionFixture(unittest.TestCase):
    """One repository for the whole class: a base, two release tags, and nine crafted PRs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp())
        repo = cls.repo = cls.tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "master")
        git(repo, "config", "user.email", "t@example.org")
        git(repo, "config", "user.name", "t")
        base = commit(repo, {"a.txt": "one\ntwo\nthree\n", "b.txt": "keep me\n", "c.txt": "unrelated\n",
                             "GUIDE.md": "# Guide\n\nNew code goes in `src/`, and every change needs a test.\n",
                             "version.txt": "db = 10\n", "routes/api.php": "<?php\n"}, "base")
        git(repo, "tag", "v1.0.0")
        base = commit(repo, {"c.txt": "unrelated, changed\n"}, "second")
        git(repo, "tag", "v1.1.0")
        git(repo, "update-ref", "refs/remotes/origin/master", base)
        cls.base = base
        pr(repo, 1, base, {"a.txt": "one\nTWO from 1\nthree\n"})
        pr(repo, 2, base, {"a.txt": "one\nTWO from 2\nthree\n"})              # conflicts with 1
        git(repo, "checkout", "-q", "--detach", "refs/pr/1")
        git(repo, "update-ref", "refs/pr/3", commit(repo, {"d.txt": "on top of 1\n"}, "PR 3"))  # built on 1
        git(repo, "checkout", "-q", "master")
        pr(repo, 4, base, {"b.txt": None})                                   # deletes b
        pr(repo, 5, base, {"b.txt": "keep me, edited\n"})                    # changes b
        pr(repo, 6, base, {"routes/api.php": "<?php\n'GET /api/widgets' => 1;\n"})
        pr(repo, 7, base, {"routes/api.php": "<?php\n// other\n", "routes/more.php": "<?php\n'GET /api/widgets' => 2;\n"})
        pr(repo, 8, base, {"version.txt": "db = 11\n", "e.txt": "x\n"})       # claims 11
        pr(repo, 9, base, {"version.txt": "db = 11\n", "f.txt": "y\n"})       # claims 11 too
        cls.numbers = list(range(1, 10))
        cls.prlist = cls.tmp / "open-prs.txt"
        cls.prlist.write_text("".join(f"{n} master\n" for n in cls.numbers))
        cls.profile = cls.tmp / "profile.json"
        cls.profile.write_text(json.dumps(PROFILE))
        cls.work = cls.tmp / "audit"
        cls.findings = cls.audit(cls.work)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def audit(cls, work: Path, master: str | None = None) -> dict:
        env = {**os.environ, "WIM_PROFILE": str(cls.profile)}
        if master:
            env["WIM_MASTER"] = master
        r = subprocess.run([sys.executable, "-I", str(MOTION / "repo_audit.py"), str(cls.repo), str(work), str(cls.prlist)],
                           capture_output=True, text=True, env=env)
        if r.returncode:
            raise AssertionError(r.stderr[-2000:])
        return json.loads((work / "findings.json").read_text())

    def test_text_conflict(self) -> None:
        self.assertIn([1, 2], [x["prs"] for x in self.findings["text_conflict"]])

    def test_stacked(self) -> None:
        self.assertIn({"contained": 1, "in": 3, "how": "built on it"}, self.findings["stacked"])

    def test_deleted_still_changed(self) -> None:
        self.assertTrue(any(x["deletes"] == 4 and x["changes"] == 5 for x in self.findings["deleted_changed"]))

    def test_shared_name(self) -> None:
        self.assertIn({"kind": "route", "name": "GET /api/widgets", "prs": [6, 7]}, self.findings["shared_name"])

    def test_version_claim_shared(self) -> None:
        self.assertEqual(self.findings["version_claims"]["shared"], {"11": [8, 9]})

    def test_exactly_the_conflicting_pairs(self) -> None:
        """1 and 2 edit one line; 3 carries 1's edit; 4 deletes what 5 edits; 6 and 7 insert at one spot."""
        pairs = {tuple(x["prs"]) for x in self.findings["text_conflict"]}
        self.assertEqual(pairs, {(1, 2), (2, 3), (4, 5), (6, 7)})

    def run_controls(self, controls: list[dict]) -> subprocess.CompletedProcess:
        path = self.tmp / "controls.json"
        path.write_text(json.dumps({"controls": controls}))
        return subprocess.run([sys.executable, "-I", str(MOTION / "controls.py"), str(self.work / "findings.json"), str(self.prlist), str(path)],
                              capture_output=True, text=True)

    def test_controls_pass_on_known_cases(self) -> None:
        r = self.run_controls([{"name": "1 and 2 conflict", "prs": [1, 2], "in": "text_conflict", "match": {"prs": [1, 2]}},
                               {"name": "3 is built on 1", "prs": [1, 3], "in": "stacked", "match": {"contained": 1, "in": 3}}])
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_wrong_control_stops_the_build(self) -> None:
        r = self.run_controls([{"name": "5 and 6 conflict (they don't)", "prs": [5, 6], "in": "text_conflict", "match": {"prs": [5, 6]}}])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("VOID", r.stdout)

    def test_incremental_rebuild_equals_full(self) -> None:
        """Base moves under the cache: only PRs touching the files it changed are merged again, and the result
        equals a build from nothing."""
        work = self.tmp / "incremental"
        old = git(self.repo, "rev-parse", "refs/remotes/origin/master")
        shutil.copytree(self.work, work)
        git(self.repo, "checkout", "-q", "--detach", old)
        moved = commit(self.repo, {"a.txt": "one\ntwo\nthree, moved\n"}, "base moves, touching a.txt")
        git(self.repo, "checkout", "-q", "master")
        try:
            inc = self.audit(work, master=moved)
            full = self.audit(self.tmp / "full", master=moved)
        finally:
            pass
        stats = inc["summary"]["incremental"]
        self.assertEqual(stats["rule_violations"], [])
        self.assertGreater(stats["pairs_reused"], 0, "pairs not touching a.txt are reused")
        r = subprocess.run([sys.executable, "-I", str(MOTION / "compare_findings.py"), str(work / "findings.json"),
                            str(self.tmp / "full" / "findings.json")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


    def page(self, quote: str) -> subprocess.CompletedProcess:
        """intent.py over the fixture's findings, with one area whose target quotes GUIDE.md."""
        g = self.tmp / "inputs"
        (g / "issues").mkdir(parents=True, exist_ok=True)
        heads = {n: git(self.repo, "rev-parse", f"refs/pr/{n}") for n in self.numbers}
        meta = [{"number": n, "title": f"PR {n}", "isDraft": False, "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-02T00:00:00Z",
                 "author": {"login": f"author{n}"}, "labels": [], "baseRefName": "master"} for n in self.numbers]
        (g / "pr-meta.json").write_text(json.dumps(meta))
        (g / "pr-bodies.json").write_text(json.dumps([{"number": n, "body": "", "reviewDecision": None} for n in self.numbers]))
        (g / "pr-extra.json").write_text(json.dumps({n: {"head": heads[n], "additions": 1, "deletions": 0, "files": 1, "author_role": "CONTRIBUTOR",
                                                         "comments": 0, "closing": [], "reviews": [], "ci": "SUCCESS"} for n in self.numbers}))
        (g / "mergers.json").write_text("{}")
        profile = {**PROFILE, "streams": {"general": [], "rules_package": None, "streams": [
            {"id": "code", "title": "Code layout", "summary": "Where code goes.", "plan": [], "before": [],
             "target": [{"text": quote, "source": "GUIDE.md", "url": "https://github.com/example/app/blob/master/GUIDE.md"}]}]}}
        path = self.tmp / "page-profile.json"
        path.write_text(json.dumps(profile))
        return subprocess.run([sys.executable, "-I", str(MOTION / "intent.py"), str(self.repo), str(self.work), str(g),
                               str(MOTION / "page.html"), str(self.tmp / "page.html")],
                              capture_output=True, text=True, env={**os.environ, "WIM_PROFILE": str(path), "WIM_OFFLINE": "1"})

    def test_page_renders_with_a_checked_quote(self) -> None:
        r = self.page("New code goes in src/, and every change needs a test.")
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        page = (self.tmp / "page.html").read_text()
        self.assertIn("<title>App Work in Motion</title>", page)
        data = json.loads((self.work / "map.json").read_text())
        self.assertEqual(data["streams"][0]["target"][0]["verified"], "verbatim")

    def test_groups_join_what_needs_a_decision(self) -> None:
        r = self.page("New code goes in src/, and every change needs a test.")
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        data = json.loads((self.work / "map.json").read_text())
        # 1 and 2 conflict and 3 is built on 1; 4 deletes what 5 changes; 6 and 7 add one route
        self.assertEqual([[1, 2, 3], [4, 5], [6, 7]], [g["prs"] for g in data["groups"]])
        kinds = {(x["a"], x["b"]): x["kinds"] for g in data["groups"] for x in g["links"]}
        self.assertIn("stacked", kinds[(1, 3)])
        self.assertIn("same name", kinds[(6, 7)])
        self.assertEqual(1, data["prs"]["1"]["group"] if "1" in data["prs"] else data["prs"][1]["group"])
        # 8 and 9 share only a version number: a renumber, not a decision, so both stand alone
        self.assertEqual({"open": 9, "alone": 2, "groups": 3, "in_groups": 7},
                         {k: data["glance"][k] for k in ("open", "alone", "groups", "in_groups")})

    def test_since_compares_with_a_build_at_least_12_hours_old(self) -> None:
        from datetime import datetime, timedelta
        hist = self.tmp / "inputs" / "history"
        shutil.rmtree(hist, ignore_errors=True)
        hist.mkdir(parents=True)
        def pr_state(**over):
            return {"t": "PR", "by": "someone", "draft": False, "ci": "SUCCESS", "rebase": False, "maint": "", "maint_by": "",
                    "shortlist": False, "head": "x", **over}
        two_days = {"at": (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds"), "master": "a" * 40, "code_pairs": [],
                    "prs": {"1": pr_state(), "2": pr_state(), "8": pr_state(shortlist=True), "99": pr_state(t="Gone PR")}}
        an_hour = {**two_days, "at": (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),
                   "prs": {**two_days["prs"], "8": pr_state(shortlist=False)}}
        (hist / "20261001T000000.json").write_text(json.dumps(two_days))
        (hist / "20261002T000000.json").write_text(json.dumps(an_hour))
        r = self.page("New code goes in src/, and every change needs a test.")
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        since = json.loads((self.work / "map.json").read_text())["since"]
        self.assertEqual(two_days["at"], since["at"], "the hour-old build is too recent to compare with")
        self.assertEqual([99], [x["n"] for x in since["gone"]], "offline, a PR no longer open is neither merged nor closed")
        self.assertIn(9, since["new"])
        self.assertEqual([8], [x["n"] for x in since["left"]])
        self.assertIn("version number", since["left"][0]["why"], "8 and 9 claim one version number")
        self.assertIn([1, 2], since["conflicts"])
        self.assertEqual(3, len(list(hist.glob("*.json"))), "this build adds its own snapshot")

    def test_a_drifted_quote_stops_the_page(self) -> None:
        r = self.page("New code goes in lib/, and every change needs a test.")
        self.assertEqual(r.returncode, 2, "a quote its source doesn't contain must stop the build")


    def test_packets_record_the_heads_they_read(self) -> None:
        cache = self.tmp / "sem"
        (cache / "audit").mkdir(parents=True, exist_ok=True)
        shutil.copy(self.work / "prs.json", cache / "audit" / "prs.json")
        meta = [{"number": n, "title": f"PR {n}", "isDraft": False, "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-02T00:00:00Z",
                 "author": {"login": f"author{n}"}, "labels": []} for n in self.numbers]
        (cache / "pr-meta.json").write_text(json.dumps(meta))
        (cache / "pr-bodies.json").write_text(json.dumps([{"number": n, "body": f"Partner is #{7 if n == 6 else 6}.\nOther line."} for n in self.numbers]))
        pair = {"a": 6, "b": 7, "score": 1, "read": True, "text_conflict": True, "stacked": False, "titles": ["PR 6", "PR 7"],
                "authors": ["author6", "author7"], "anchors": [{"kind": "route family", "name": "/api/widgets", "a": ["adds routes"], "b": ["adds routes"], "weight": 2}]}
        (cache / "candidates.json").write_text(json.dumps({"master": self.base, "pairs": 1, "candidates": [pair]}))
        r = subprocess.run([sys.executable, "-I", str(MOTION / "semantic_packets.py"), str(self.repo), str(cache)],
                           capture_output=True, text=True, env={**os.environ, "WIM_PROFILE": str(self.profile)})
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        heads = json.loads((cache / "semantic" / "heads.json").read_text())
        self.assertEqual(heads["pairs"]["6-7"], {"6": git(self.repo, "rev-parse", "refs/pr/6"), "7": git(self.repo, "rev-parse", "refs/pr/7")})
        packet = (cache / "semantic" / "packets" / "6-7" / "pr-6.md").read_text()
        self.assertIn("[a line naming #7 was withheld", packet, "the partner's mention is withheld")
        self.assertNotIn("Partner is #7", packet)


HAVE_PHP = bool(shutil.which("php") or shutil.which("docker"))


@unittest.skipUnless(HAVE_PHP, "needs php or docker for PHP's tokenizer")
class PhpSignatures(unittest.TestCase):
    def test_an_override_php_would_refuse(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            repo = tmp / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "master")
            git(repo, "config", "user.email", "t@example.org")
            git(repo, "config", "user.name", "t")
            base = commit(repo, {"src/Base.php": "<?php\nabstract class Base\n{\n    abstract public function make($x);\n}\n"}, "base")
            git(repo, "update-ref", "refs/remotes/origin/master", base)
            pr(repo, 10, base, {"src/Base.php": "<?php\nabstract class Base\n{\n    abstract public function make($x): array;\n}\n"})
            pr(repo, 11, base, {"src/Child.php": "<?php\nclass Child extends Base\n{\n    public function make($x)\n    {\n        return [];\n    }\n}\n"})
            (tmp / "prs.txt").write_text("10 master\n11 master\n")
            (tmp / "profile.json").write_text(json.dumps({**PROFILE, "symbols": "php", "mechanical": "$^"}))
            r = subprocess.run([sys.executable, "-I", str(MOTION / "repo_audit.py"), str(repo), str(tmp / "audit"), str(tmp / "prs.txt")],
                               capture_output=True, text=True, env={**os.environ, "WIM_PROFILE": str(tmp / "profile.json")})
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])
            found = json.loads((tmp / "audit" / "findings.json").read_text())["signature"]
            self.assertTrue(any(x["changes"] == 10 and x["overrides"] == 11 and x["method"] == "Base::make" for x in found), found)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Skill(unittest.TestCase):
    """The Claude Code skill must describe the tool as it is."""

    def test_frontmatter_and_subcommands(self) -> None:
        text = (ROOT / "skills" / "pr-toolbox-motion" / "SKILL.md").read_text()
        front = text.split("---")[1]
        self.assertIn("name: pr-toolbox-motion", front)
        self.assertRegex(front, r"description: .{100,}")
        named = set(re.findall(r"(?:^|`)pr-toolbox motion ([a-z][a-z-]*)\b", text, re.M))  # commands in code, not prose
        # the subcommands motion's command line accepts, read from its own source rather than listed here
        real = set(re.findall(r'"([a-z-]+)"', re.search(r"argv\[0\] in \{([^}]*)\}", (ROOT / "pr_toolbox/motion/cli.py").read_text()).group(1)))
        self.assertIn("check-page", real)
        self.assertTrue(named, "the skill names its commands")
        self.assertLessEqual(named - {"compare", "build"}, real, f"unknown subcommands: {named - real}")
        for path in re.findall(r"`(pr_toolbox/[\w/.-]+)`", text):
            self.assertTrue((ROOT / path).exists(), f"the skill names {path}, which doesn't exist")


def _plain_strings_with_templates(js: str) -> list[str]:
    """Double-quoted JavaScript strings that contain "${": they print the dollar sign and braces, where a template
    literal was meant. Walks strings, template literals and their ${...} expressions, comments and regex literals."""
    found, stack, prev, i, n = [], ["code"], "", 0, len(js)
    while i < n:
        c, top = js[i], stack[-1]
        if top == "template":
            if c == "\\":
                i += 2
            elif c == "`":
                stack.pop()
                prev, i = "`", i + 1
            elif js.startswith("${", i):
                stack.append("expr")
                prev, i = "{", i + 2
            else:
                i += 1
            continue
        if js.startswith("//", i):
            i = js.find("\n", i) if "\n" in js[i:] else n
            continue
        if js.startswith("/*", i):
            i = js.index("*/", i + 2) + 2
            continue
        if c in "\"'":
            j = i + 1
            while j < n and js[j] not in (c, "\n"):
                j += 2 if js[j] == "\\" else 1
            if c == '"' and "${" in js[i:j + 1]:
                found.append(js[i:j + 1])
            prev, i = c, j + 1
            continue
        if c == "`":
            stack.append("template")
            i += 1
            continue
        if c == "/" and (not prev or prev in "(,=:[!&|?{};+-*%<>~^"):
            j, in_class = i + 1, False
            while j < n and js[j] != "\n" and (in_class or js[j] != "/"):
                in_class = (in_class or js[j] == "[") and js[j] != "]"
                j += 2 if js[j] == "\\" else 1
            prev, i = "/", j + 1
            continue
        if c == "{":
            stack.append("brace")
        elif c == "}" and top in ("expr", "brace"):
            stack.pop()
        if not c.isspace():
            prev = c
        i += 1
    return found


class CiState(unittest.TestCase):
    """motion and health must read CI the same way: Codecov and the summary job left out, cancelled runs not failures."""

    @classmethod
    def setUpClass(cls) -> None:
        import ast
        tree = ast.parse((MOTION / "fetch.py").read_text())
        keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == "ci_state")
                or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in {"FAILED", "PENDING"} for t in n.targets))]
        cls.ns = {"re": re}
        exec(compile(ast.Module(body=keep, type_ignores=[]), "fetch.py", "exec"), cls.ns)

    def read(self, ignore: str, *checks, total=None):
        nodes = [{"__typename": "CheckRun", "name": n, "conclusion": c if c not in {"IN_PROGRESS"} else None,
                  "status": "IN_PROGRESS" if c == "IN_PROGRESS" else "COMPLETED"} for n, c in checks]
        self.ns["CI_IGNORE"] = re.compile(ignore)
        rollup = {"state": "FAILURE", "contexts": {"totalCount": total or len(nodes), "nodes": nodes}}
        return self.ns["ci_state"](rollup)

    def test_codecov_and_the_summary_job_are_left_out(self) -> None:
        ignore = "^(codecov/.*|All Checks Passed)$"
        self.assertEqual(("SUCCESS", []), self.read(ignore, ("codecov/project", "FAILURE"), ("All Checks Passed", "FAILURE"), ("phpstan", "SUCCESS")))
        self.assertEqual(("FAILURE", ["api"]), self.read(ignore, ("api", "FAILURE"), ("PR Title", "CANCELLED"), ("All Checks Passed", "FAILURE")))
        self.assertEqual(("PENDING", []), self.read(ignore, ("api", "IN_PROGRESS"), ("phpstan", "SUCCESS")))
        self.assertEqual(("FAILURE", []), self.read(ignore, ("api", "SUCCESS"), total=120), "more checks than fetched: GitHub's rollup")

    def test_motion_agrees_with_health(self) -> None:
        from pr_toolbox import pr_health
        ignore = "^(codecov/.*|All Checks Passed)$"
        cases = [[("codecov/patch", "FAILURE"), ("phpstan", "SUCCESS")], [("phpstan", "FAILURE"), ("e2e", "CANCELLED")],
                 [("e2e", "IN_PROGRESS")], [("All Checks Passed", "FAILURE"), ("lint", "TIMED_OUT")]]
        words = {"SUCCESS": "passing", "FAILURE": "failing", "PENDING": "pending"}
        for checks in cases:
            state, failing = self.read(ignore, *checks)
            node = {"title": "t", "url": "u", "isDraft": False, "headRefOid": "h", "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                "contexts": {"nodes": [{"name": n, "conclusion": None if c == "IN_PROGRESS" else c, "status": "IN_PROGRESS" if c == "IN_PROGRESS" else "COMPLETED"} for n, c in checks]}}}}]}}
            health = pr_health.summarize(node, "me", re.compile(ignore))
            self.assertEqual((health["ci"], health["failing"]), (words[state], failing), checks)


class CheckPage(unittest.TestCase):
    """check-page runs the browser checks with this Python's Playwright, else in Playwright's image, else says what it needs."""

    def setUp(self) -> None:
        self.page = Path(tempfile.mkdtemp()) / "page.html"
        self.page.write_text("<html></html>")

    def test_a_missing_page_is_named(self) -> None:
        from unittest import mock
        with mock.patch("sys.stderr") as err:
            self.assertEqual(2, cli.main(["check-page", str(self.page.parent / "nope.html")]))
        self.assertIn("no page at", "".join(c.args[0] for c in err.write.call_args_list))

    def test_without_playwright_or_docker_it_says_what_it_needs(self) -> None:
        from unittest import mock
        with mock.patch.object(cli.importlib.util, "find_spec", return_value=None), mock.patch.object(cli.shutil, "which", return_value=None), \
                mock.patch("sys.stderr") as err:
            self.assertEqual(2, cli.main(["check-page", str(self.page)]))
        self.assertIn("needs Playwright", "".join(c.args[0] for c in err.write.call_args_list))

    def test_with_docker_it_runs_playwrights_image_on_the_page_read_only(self) -> None:
        from unittest import mock
        with mock.patch.object(cli.importlib.util, "find_spec", return_value=None), mock.patch.object(cli.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(0, cli.main(["check-page", str(self.page)]))
        cmd = run.call_args.args[0]
        self.assertEqual(["docker", "run", "--rm"], cmd[:3])
        self.assertIn(f"{self.page.resolve()}:/page/index.html:ro", cmd)
        self.assertIn(cli.PLAYWRIGHT_IMAGE, cmd)


class Profiles(unittest.TestCase):
    def test_openemr_preset_carries_motion(self) -> None:
        from pr_toolbox import common
        cfg = common.load_config(Path("."), "openemr/openemr", preset="openemr")
        target = common.Target(Path("."), "upstream", "openemr/openemr", "master", cfg)
        p = cli.resolve_profile(target, None)
        self.assertEqual(p["title"], "OpenEMR Work in Motion")
        self.assertTrue(p["streams"]["streams"], "OpenEMR's areas come with the preset")
        self.assertTrue(p["controls"]["controls"], "and its controls")
        self.assertTrue(all("url" in t for s in p["streams"]["streams"] for t in s["target"]), "every target cites its source")

    def test_the_page_names_the_release_that_built_it(self) -> None:
        from pr_toolbox import __version__, common
        target = common.Target(Path("."), "origin", "someone/thing", "main", {})
        self.assertEqual(cli.resolve_profile(target, None)["tool_version"], __version__)

    def test_the_template_names_no_project(self) -> None:
        # the page is every project's: OpenEMR's PRs and labels come from its profile, never from the template
        page = (MOTION / "page.html").read_text()
        self.assertIsNone(re.search(r"prLink\(\d+\)|isLink\(\d+\)|#1\d{4}\b", page))
        self.assertNotIn("OpenEMR", page)
        self.assertNotIn("ACL", page)
        for name in ("OEGlobalsBag", "QueryUtils", "v_database", "AGENTS.md"):  # names from the OpenEMR profile
            self.assertNotIn(name, page)

    def test_no_template_expression_in_a_plain_string(self) -> None:
        page = (MOTION / "page.html").read_text()
        script = page[page.index("<script>") + 8:page.rindex("</script>")]
        self.assertEqual([], _plain_strings_with_templates(script))
        # the check catches the bug it was written for, and not its look-alikes
        self.assertEqual(['" (${esc(BASE)} already uses it)"'], _plain_strings_with_templates('x ? " (${esc(BASE)} already uses it)" : ""'))
        self.assertEqual([], _plain_strings_with_templates("""`a ${p.x ? "b" : ""}${f("c")} d` + "e" + /[&<>"']/g.source"""))

    def test_neutral_defaults_for_any_repository(self) -> None:
        from pr_toolbox import common
        target = common.Target(Path("."), "origin", "someone/thing", "main", {})
        p = cli.resolve_profile(target, None)
        self.assertEqual(p["title"], "thing Work in Motion")
        self.assertEqual(p["mechanical"], "$^", "no rules, no mechanical files")
        self.assertEqual(p["streams"]["streams"], [])

    def test_mechanical_files_follow_the_rules(self) -> None:
        from pr_toolbox import common
        cfg = {"version_claims": {"file": "VERSION", "related": ["VERSION", "db/*.sql"]}}
        p = cli.resolve_profile(common.Target(Path("."), "origin", "a/b", "main", cfg), None)
        self.assertRegex("db/upgrade.sql", p["mechanical"])
        self.assertNotRegex("src/app.py", p["mechanical"])


if __name__ == "__main__":
    unittest.main()

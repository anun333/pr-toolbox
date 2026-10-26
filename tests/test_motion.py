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


    def page(self, quote: str, conformance: dict | None = None) -> subprocess.CompletedProcess:
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
        if conformance is not None:
            profile["conformance"] = conformance
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

    def test_the_conformance_lens_matches_prs_and_its_controls_stop_a_bad_build(self) -> None:
        conf = {"specs": [{"id": "std", "label": "(x)(1)", "title": "A standard", "certified": True, "paths": ["a.txt"],
                           "tool": {"name": "a validator", "before_merge": False}, "hti5": {"action": "kept"}}],
                "controls": [{"files": ["a.txt"], "expect": ["std"]}, {"files": ["c.txt"], "expect": []}]}
        r = self.page("New code goes in src/, and every change needs a test.", conformance=conf)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        data = json.loads((self.work / "map.json").read_text())
        matched = {x["n"] for x in data["conformance"]["specs"][0]["inflight"]}
        self.assertTrue({1, 2} <= matched, matched)
        self.assertIn("std", data["prs"]["1"]["conf"])
        self.assertNotIn("title_re", data["conformance"]["specs"][0])
        # a path no file on the base starts with stops the build, and so does a control that matches wrongly
        for bad in ({**conf, "specs": [{**conf["specs"][0], "paths": ["a.txt", "nowhere/"]}]},
                    {**conf, "controls": [{"files": ["c.txt"], "expect": ["std"]}]}):
            r = self.page("New code goes in src/, and every change needs a test.", conformance=bad)
            self.assertEqual(r.returncode, 2, r.stderr[-800:])
            self.assertIn("conformance control", r.stderr)

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
                    "prs": {"1": pr_state(), "2": pr_state(), "8": pr_state(shortlist=True), "99": pr_state(t="Gone PR"), "98": pr_state(t="Retargeted")}}
        an_hour = {**two_days, "at": (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),
                   "prs": {**two_days["prs"], "8": pr_state(shortlist=False)}}
        (hist / "20261001T000000.json").write_text(json.dumps(two_days))
        (hist / "20261002T000000.json").write_text(json.dumps(an_hour))
        # 98 is still open, now aimed at a release branch: it left the list, but it was neither merged nor closed
        (self.tmp / "inputs" / "other-bases.json").write_text(json.dumps([{"number": 98, "title": "Retargeted", "base": "release/1.0", "by": "someone"}]))
        r = self.page("New code goes in src/, and every change needs a test.")
        (self.tmp / "inputs" / "other-bases.json").unlink()
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        since = json.loads((self.work / "map.json").read_text())["since"]
        self.assertEqual(two_days["at"], since["at"], "the hour-old build is too recent to compare with")
        self.assertEqual([99], [x["n"] for x in since["gone"]], "offline, a PR no longer open is neither merged nor closed")
        self.assertEqual([(98, "release/1.0")], [(x["n"], x["base"]) for x in since["elsewhere"]], "a PR aimed elsewhere is still open")
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

    def read(self, ignore: str, *checks, total=None, required=None):
        nodes = [{"__typename": "CheckRun", "name": n, "conclusion": c if c not in {"IN_PROGRESS"} else None,
                  "status": "IN_PROGRESS" if c == "IN_PROGRESS" else "COMPLETED"} for n, c in checks]
        self.ns["CI_IGNORE"] = re.compile(ignore)
        self.ns["CI_REQUIRED"] = re.compile(required) if required else None
        rollup = {"state": "FAILURE", "contexts": {"totalCount": total or len(nodes), "nodes": nodes}}
        return self.ns["ci_state"](rollup)

    def test_ci_that_never_ran_is_not_passing(self) -> None:
        # nextcloud/server#61279 on 2026-10-21: a fork PR whose workflows wait for a maintainer's approval carries
        # only the DCO app's check and a skipped job
        required = "^(static-code-analysis|PHPUnit)"
        not_run = [("DCO", "SUCCESS"), ("auto-approve-merge", "SKIPPED")]
        self.assertEqual((None, []), self.read("^codecov/", *not_run, required=required))
        self.assertEqual(("SUCCESS", []), self.read("^codecov/", *not_run, ("static-code-analysis", "SUCCESS"), required=required),
                         "control: the required check is present")
        self.assertEqual(("FAILURE", ["DCO"]), self.read("^codecov/", ("DCO", "FAILURE"), required=required),
                         "a failing check still reads as failing")
        self.assertEqual(("SUCCESS", []), self.read("^codecov/", *not_run), "no ci_required in the profile: read as before")

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


class FetchPages(unittest.TestCase):
    """The PR list survives GitHub's 10-second limit, and PRs aimed at another branch are set apart."""

    @classmethod
    def setUpClass(cls) -> None:
        import ast
        tree = ast.parse((MOTION / "fetch.py").read_text())
        keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"read_pages", "split_by_base", "landed_by", "quote_raw_url",
                                                                                     "quote_source_for", "clean_quoted"}]
        cls.ns = {"json": json, "re": re}
        exec(compile(ast.Module(body=keep, type_ignores=[]), "fetch.py", "exec"), cls.ns)

    def test_a_page_that_times_out_is_asked_for_at_half_the_size(self) -> None:
        # decidim/decidim on 2026-10-22: 25 PRs with about 40 checks each took 9.8s, and some pages came back 504
        asked = []

        def call(size, cursor):
            asked.append((size, cursor))
            if size > 10:
                return 1, "", "gh: HTTP 504"
            last = cursor == "c1"
            page = {"nodes": [{"number": 2 if last else 1}], "pageInfo": {"hasNextPage": not last, "endCursor": "c1"}}
            return 0, json.dumps({"data": {"repository": {"pullRequests": page}}}), ""
        self.assertEqual([1, 2], [p["number"] for p in self.ns["read_pages"](call)])
        self.assertEqual([(25, None), (12, None), (6, None), (6, "c1")], asked)

    def test_a_page_that_keeps_failing_stops_the_build(self) -> None:
        with self.assertRaises(SystemExit) as stop:
            self.ns["read_pages"](lambda size, cursor: (1, "", "gh: HTTP 401 Bad credentials"))
        self.assertIn("401", str(stop.exception))

    def test_a_search_window_read_short_is_read_again(self) -> None:
        import ast
        from datetime import date, timedelta
        tree = ast.parse((MOTION / "fetch.py").read_text())
        keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"merged_between", "landed_by"}]
        reads = []

        def run(*args):
            # three merges in the window; the first read ends early, as GitHub's search did, with an empty page counting 0
            cursor = next((a.split("=", 1)[1] for a in args if a.startswith("cursor=")), None)
            reads.append(cursor)
            short = reads.count(None) == 1
            if cursor is None:
                page = {"issueCount": 3, "nodes": [{"mergedBy": {"login": "a"}}, {"mergedBy": {"login": "b"}}], "pageInfo": {"hasNextPage": True, "endCursor": "c"}}
            else:
                page = {"issueCount": 0 if short else 3, "nodes": [] if short else [{"mergedBy": {"login": "a"}}], "pageInfo": {"hasNextPage": False, "endCursor": None}}
            return json.dumps({"data": {"search": page}})
        ns = {"json": json, "timedelta": timedelta, "run": run, "P": {"github": "o/r"}, "MERGED": "", "mergers": {}}
        exec(compile(ast.Module(body=keep, type_ignores=[]), "fetch.py", "exec"), ns)
        ns["merged_between"](date(2026, 1, 1), date(2026, 1, 1))
        self.assertEqual({"a": 2, "b": 1}, ns["mergers"], "the short read counts nothing; the whole one counts once")
        self.assertEqual([None, "c", None, "c"], reads)

        # the first page itself can be wrong: a count of 0, some merges, and no next page
        firsts = []

        def first_page_wrong(*args):
            firsts.append(1)
            whole = len(firsts) > 1
            page = {"issueCount": 2 if whole else 0, "nodes": [{"mergedBy": {"login": "a"}}] * (2 if whole else 1), "pageInfo": {"hasNextPage": False, "endCursor": None}}
            return json.dumps({"data": {"search": page}})
        ns.update(run=first_page_wrong, mergers={})
        exec(compile(ast.Module(body=keep, type_ignores=[]), "fetch.py", "exec"), ns)
        ns["merged_between"](date(2026, 1, 1), date(2026, 1, 1))
        self.assertEqual({"a": 2}, ns["mergers"])

        def always_short(*args):
            first = not any(a.startswith("cursor=") for a in args)
            page = {"issueCount": 3 if first else 0, "nodes": [], "pageInfo": {"hasNextPage": first, "endCursor": "c"}}
            return json.dumps({"data": {"search": page}})
        ns.update(run=always_short, mergers={})
        exec(compile(ast.Module(body=keep, type_ignores=[]), "fetch.py", "exec"), ns)
        with self.assertRaises(SystemExit, msg="a single day that stays short stops the build"):
            ns["merged_between"](date(2026, 1, 1), date(2026, 1, 1))

    def test_prs_aimed_at_another_branch_are_set_apart(self) -> None:
        prs = [{"number": 1, "baseRefName": "develop"}, {"number": 2, "baseRefName": "release/0.31-stable"}, {"number": 3, "baseRefName": "develop"}]
        on, off = self.ns["split_by_base"](prs, "develop")
        self.assertEqual(([1, 3], [2]), ([p["number"] for p in on], [p["number"] for p in off]))

    def test_prs_on_a_branch_merged_up_are_kept(self) -> None:
        # php-src: PHP-8.4 is merged up into master after every change; PHP-8.1 no longer is
        prs = [{"number": 1, "baseRefName": "master"}, {"number": 2, "baseRefName": "PHP-8.4"}, {"number": 3, "baseRefName": "PHP-8.1"}]
        on, off = self.ns["split_by_base"](prs, "master", ["PHP-8.4", "PHP-8.5"])
        self.assertEqual(([1, 2], [3]), ([p["number"] for p in on], [p["number"] for p in off]))

    def test_a_pr_closed_by_a_commit_was_landed_by_its_committer(self) -> None:
        by_commit = {"timelineItems": {"nodes": [{"closer": {"__typename": "Commit", "committer": {"user": {"login": "bukka"}}}}]}}
        by_hand = {"timelineItems": {"nodes": [{"closer": None}]}}
        by_pr = {"timelineItems": {"nodes": [{"closer": {"__typename": "PullRequest"}}]}}
        landed = self.ns["landed_by"]
        self.assertEqual(("bukka", None, None), (landed(by_commit, "closed"), landed(by_hand, "closed"), landed(by_pr, "closed")))
        self.assertEqual("girgias", landed({"mergedBy": {"login": "girgias"}}, "merged"))
        self.assertIsNone(landed(by_commit, "merged"), "a merge search reads the merger, never the closing commit")

    def test_a_quote_outside_the_repository_reads_its_raw_text(self) -> None:
        sources = [{"match": r"^https://wiki\.php\.net/(rfc/[^?#]+)", "raw": "https://wiki.php.net/{0}?do=export_raw"}]
        raw = self.ns["quote_raw_url"]
        self.assertEqual("https://wiki.php.net/rfc/release_cycle_update?do=export_raw",
                         raw("https://wiki.php.net/rfc/release_cycle_update#extend_security_support_by_one_year", sources))
        self.assertIsNone(raw("https://github.com/php/php-src/issues/1", sources))
        self.assertIsNone(raw("https://wiki.php.net/rfc/x", []))

    def test_a_quoted_wiki_page_is_matched_as_it_reads(self) -> None:
        wiki = {"clean": [[r"\[\[[^|\]]*\|\s*([^\]]*)\]\]", r"\1"], [r"''", ""], [r"\*\*", ""]]}
        text = "This RFC proposes a set of follow-up improvements to the [[rfc:url_parsing_api|URL Parsing API RFC]]. It extends the ''Uri\\Rfc3986\\Uri'' class; it **must** be merged."
        self.assertEqual("This RFC proposes a set of follow-up improvements to the URL Parsing API RFC. It extends the Uri\\Rfc3986\\Uri class; it must be merged.",
                         self.ns["clean_quoted"](text, wiki))
        self.assertEqual(text, self.ns["clean_quoted"](text, {}), "a source without clean rules is read as it is")


class FetchHeads(unittest.TestCase):
    """A PR GitHub lists with no refs/pull/<n>/head is fetched from its merge ref, or from its head repository, and
    only at the head GitHub reports."""

    def test_a_pr_without_a_head_ref(self) -> None:
        import ast
        tree = ast.parse((MOTION / "fetch.py").read_text())
        ns = {"subprocess": subprocess}
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "fetch_head"], type_ignores=[]),
                     "fetch.py", "exec"), ns)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        work, hub, fork, clone = tmp / "work", tmp / "hub.git", tmp / "fork", tmp / "clone"
        work.mkdir()
        git(work, "init", "-q", "-b", "master")
        git(work, "config", "user.email", "t@example.org")
        git(work, "config", "user.name", "t")
        base = commit(work, {"a.txt": "a\n"}, "base")
        head7 = commit(work, {"b.txt": "seven\n"}, "PR 7")
        git(work, "checkout", "-q", "--detach", base)
        merge7 = git(work, "commit-tree", git(work, "rev-parse", f"{head7}^{{tree}}"), "-p", base, "-p", head7, "-m", "merge 7")
        head8 = commit(work, {"c.txt": "eight\n"}, "PR 8")
        old8 = git(work, "rev-parse", "HEAD~0")
        moved8 = commit(work, {"c.txt": "eight, pushed again\n"}, "PR 8, pushed again")
        stale8 = git(work, "commit-tree", git(work, "rev-parse", f"{base}^{{tree}}"), "-p", base, "-p", old8, "-m", "stale merge 8")
        subprocess.run(["git", "init", "-q", "--bare", str(hub)], check=True)
        git(work, "push", "-q", str(hub), f"{base}:refs/heads/master", f"{merge7}:refs/pull/7/merge", f"{stale8}:refs/pull/8/merge",
            f"{old8}:refs/pull/9/head")  # #9's head ref kept at an older commit than its real head, as #21778's was
        subprocess.run(["git", "clone", "-q", str(work), str(fork)], check=True)  # the author's fork, which has every commit
        git(fork, "update-ref", "refs/heads/pr8", moved8)
        subprocess.run(["git", "clone", "-q", str(hub), str(clone)], check=True)
        del head8
        self.assertTrue(ns["fetch_head"](clone, "origin", 7, head7), "from the merge ref's second parent")
        self.assertEqual(head7, git(clone, "rev-parse", "refs/pr/7"))
        self.assertFalse(ns["fetch_head"](clone, "origin", 8, moved8), "a merge ref built on an older head is not the head")
        self.assertTrue(ns["fetch_head"](clone, "origin", 8, moved8, str(fork)), "from the head repository, by the reported head")
        self.assertEqual(moved8, git(clone, "rev-parse", "refs/pr/8"))
        fresh = tmp / "fresh"  # a clone that hasn't seen #8's head yet
        subprocess.run(["git", "clone", "-q", str(hub), str(fresh)], check=True, capture_output=True)
        self.assertFalse(ns["fetch_head"](fresh, "origin", 9, moved8), "a head ref behind the reported head is not the head")
        self.assertTrue(ns["fetch_head"](fresh, "origin", 9, moved8, str(fork)))
        self.assertEqual(moved8, git(fresh, "rev-parse", "refs/pr/9"))


class FarBases(unittest.TestCase):
    """Two PRs compared branch to branch, whose bases are far apart: a conflict in a file neither changes is the history
    between the bases, not their work (php-src #7173 against #24188: 95 such files)."""

    def test_only_files_both_change(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "master")
        git(repo, "config", "user.email", "t@example.org")
        git(repo, "config", "user.name", "t")
        root = commit(repo, {"ci.yml": "h\n", "f.c": "1\n2\n3\n4\n5\n"}, "base")
        git(repo, "checkout", "-q", "-b", "R-1")
        commit(repo, {"ci.yml": "r\n"}, "CI changed on the branch")
        git(repo, "checkout", "-q", "master")
        before_merge = commit(repo, {"ci.yml": "m\n"}, "CI changed on master")
        subprocess.run(["git", "-C", str(repo), "merge", "-q", "--no-commit", "R-1"], capture_output=True)
        commit(repo, {"ci.yml": "m and r\n"}, "Merge branch 'R-1', conflict resolved")
        commit(repo, {"f.c": "1 on master\n2\n3\n4\n5\n"}, "master moves on in f.c")
        git(repo, "update-ref", "refs/remotes/origin/master", git(repo, "rev-parse", "master"))
        git(repo, "update-ref", "refs/remotes/origin/R-1", git(repo, "rev-parse", "R-1"))
        pr(repo, 31, before_merge, {"f.c": "1 from 31\n2\n3\n4\n5\n"})   # conflicts with master in f.c
        pr(repo, 32, "R-1", {"f.c": "1\n2\n3\n4\n5 from 32\n"})          # on the branch; shares f.c with 31
        del root
        prlist = tmp / "open-prs.txt"
        prlist.write_text("31 master\n32 R-1\n")
        profile = tmp / "profile.json"
        profile.write_text(json.dumps({**PROFILE, "version_claims": None, "mechanical": "$^", "names": [], "name_paths": [], "merge_up": ["R-1"]}))
        r = subprocess.run([sys.executable, "-I", str(MOTION / "repo_audit.py"), str(repo), str(tmp / "audit"), str(prlist)],
                           capture_output=True, text=True, env={**os.environ, "WIM_PROFILE": str(profile)})
        self.assertEqual(0, r.returncode, r.stderr[-1500:])
        findings = json.loads((tmp / "audit" / "findings.json").read_text())
        pairs = json.loads((tmp / "audit" / "pairs.json").read_text())["pairs"]
        (entry,) = pairs.values()
        self.assertEqual(("branch to branch", ["ci.yml"]), (entry["how"], entry["files"]), "git does conflict in ci.yml")
        self.assertEqual([], findings["text_conflict"], "but neither PR changes it")

    def test_a_conflict_only_in_mechanical_files_is_paired_on_the_base(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "master")
        git(repo, "config", "user.email", "t@example.org")
        git(repo, "config", "user.name", "t")
        root = commit(repo, {"NEWS": "- one\n", "a.c": "1\n2\n3\n4\n5\n"}, "base")
        git(repo, "update-ref", "refs/remotes/origin/master", commit(repo, {"NEWS": "- one\n- from master\n"}, "a NEWS entry lands"))
        pr(repo, 41, root, {"NEWS": "- one\n- from 41\n", "a.c": "1 from 41\n2\n3\n4\n5\n"})   # NEWS conflicts with master
        pr(repo, 42, "master", {"a.c": "1\n2\n3\n4\n5 from 42\n"})
        prlist = tmp / "open-prs.txt"
        prlist.write_text("41 master\n42 master\n")
        out = {}
        for mode in ("stop", "resolve"):
            profile = tmp / f"{mode}.json"
            profile.write_text(json.dumps({**PROFILE, "version_claims": None, "mechanical": "^NEWS$", "names": [], "name_paths": [],
                                           "mechanical_conflicts": mode}))
            r = subprocess.run([sys.executable, "-I", str(MOTION / "repo_audit.py"), str(repo), str(tmp / mode), str(prlist)],
                               capture_output=True, text=True, env={**os.environ, "WIM_PROFILE": str(profile)})
            self.assertEqual(0, r.returncode, r.stderr[-1500:])
            (entry,) = json.loads((tmp / mode / "pairs.json").read_text())["pairs"].values()
            out[mode] = (entry["how"], json.loads((tmp / mode / "findings.json").read_text())["summary"]["conflict_with_master"])
        self.assertEqual(("branch to branch", [41]), out["stop"])
        self.assertEqual(("onto master", [41]), out["resolve"], "still needs a rebase; its pair is merged on master")
        # the same work directory, rebuilt under the other mode: nothing merged under the first is reused
        r = subprocess.run([sys.executable, "-I", str(MOTION / "repo_audit.py"), str(repo), str(tmp / "stop"), str(prlist)],
                           capture_output=True, text=True, env={**os.environ, "WIM_PROFILE": str(tmp / "resolve.json")})
        self.assertEqual(0, r.returncode, r.stderr[-1500:])
        (entry,) = json.loads((tmp / "stop" / "pairs.json").read_text())["pairs"].values()
        self.assertEqual("onto master", entry["how"])


class StaleConflicts(unittest.TestCase):
    """A code conflict found branch to branch, where one PR no longer merges onto the base, joins a group and blocks its
    partner by default; with "stale_conflicts": "apart" it is shown on both cards and does neither."""

    def test_apart(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "master")
        git(repo, "config", "user.email", "t@example.org")
        git(repo, "config", "user.name", "t")
        root = commit(repo, {"f.c": "1\n2\n3\n", "g.c": "a\nb\n"}, "base")
        git(repo, "update-ref", "refs/remotes/origin/master", commit(repo, {"g.c": "a on master\nb\n"}, "master moves in g.c"))
        pr(repo, 51, root, {"f.c": "1 from 51\n2\n3\n", "g.c": "a from 51\nb\n"})   # stale: conflicts with master in g.c
        pr(repo, 52, "master", {"f.c": "1 from 52\n2\n3\n"})                         # fresh, and conflicts with 51 in f.c
        prlist = tmp / "open-prs.txt"
        prlist.write_text("51 master\n52 master\n")
        g = tmp / "inputs"
        (g / "issues").mkdir(parents=True)
        heads = {n: git(repo, "rev-parse", f"refs/pr/{n}") for n in (51, 52)}
        (g / "pr-meta.json").write_text(json.dumps([{"number": n, "title": f"PR {n}", "isDraft": False, "createdAt": "2026-10-01T00:00:00Z",
                                                     "updatedAt": "2026-10-02T00:00:00Z", "author": {"login": f"a{n}"}, "labels": [],
                                                     "baseRefName": "master"} for n in (51, 52)]))
        (g / "pr-bodies.json").write_text(json.dumps([{"number": n, "body": "", "reviewDecision": None} for n in (51, 52)]))
        (g / "pr-extra.json").write_text(json.dumps({n: {"head": heads[n], "additions": 1, "deletions": 0, "files": 1, "author_role": "CONTRIBUTOR",
                                                         "comments": 0, "closing": [], "reviews": [], "ci": "SUCCESS"} for n in (51, 52)}))
        (g / "mergers.json").write_text("{}")
        out = {}
        for mode in ("link", "apart"):
            profile = tmp / f"{mode}.json"
            profile.write_text(json.dumps({**PROFILE, "version_claims": None, "mechanical": "$^", "names": [], "name_paths": [], "stale_conflicts": mode}))
            env = {**os.environ, "WIM_PROFILE": str(profile), "WIM_OFFLINE": "1"}
            work = tmp / f"audit-{mode}"
            r = subprocess.run([sys.executable, "-I", str(MOTION / "repo_audit.py"), str(repo), str(work), str(prlist)], capture_output=True, text=True, env=env)
            self.assertEqual(0, r.returncode, r.stderr[-1500:])
            r = subprocess.run([sys.executable, "-I", str(MOTION / "intent.py"), str(repo), str(work), str(g), str(MOTION / "page.html"), str(tmp / f"{mode}.html")],
                               capture_output=True, text=True, env=env)
            self.assertEqual(0, r.returncode, r.stderr[-1500:])
            m = json.loads((work / "map.json").read_text())
            out[mode] = ([c["kind"] for c in m["prs"]["52"]["conflicts"]], len(m["groups"]), m["prs"]["52"]["shortlist"])
        self.assertEqual((["code"], 1, False), out["link"])
        self.assertEqual((["after a rebase"], 0, True), out["apart"], "shown on the card, but #51 must rebase before it matters")


class StaleUpgrade(unittest.TestCase):
    """Upgrade SQL only in an earlier release's file, after a branch cut, keeps a PR out of Nothing blocking; the same
    file changed alongside the current one is shown on the card but blocks nothing."""

    def test_only_earlier_file_blocks(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "master")
        git(repo, "config", "user.email", "t@example.org")
        git(repo, "config", "user.name", "t")
        old, cur = "sql/8_4_1-to-8_5_0_upgrade.sql", "sql/8_5_0-to-8_6_0_upgrade.sql"
        root = commit(repo, {"version.php": "$v_major = '8';\n$v_minor = '5';\n$v_patch = '0';\n", old: "-- 8.5.0\n"}, "8.5.0 dev")
        cut = commit(repo, {"version.php": "$v_major = '8';\n$v_minor = '6';\n$v_patch = '0';\n", cur: "-- 8.6.0\n"}, "branch cut")
        git(repo, "update-ref", "refs/remotes/origin/master", cut)
        pr(repo, 71, root, {old: "-- 8.5.0\nALTER TABLE a ADD b int;\n"})                                # written before the cut
        pr(repo, 72, "master", {old: "-- 8.5.0\nfix the old path;\n", cur: "-- 8.6.0\nALTER TABLE a ADD c int;\n"})
        pr(repo, 73, "master", {cur: "-- 8.6.0\nALTER TABLE a ADD d int;\n"})
        numbers = (71, 72, 73)
        prlist = tmp / "open-prs.txt"
        prlist.write_text("".join(f"{n} master\n" for n in numbers))
        g = tmp / "inputs"
        (g / "issues").mkdir(parents=True)
        heads = {n: git(repo, "rev-parse", f"refs/pr/{n}") for n in numbers}
        (g / "pr-meta.json").write_text(json.dumps([{"number": n, "title": f"PR {n}", "isDraft": False, "createdAt": "2026-10-01T00:00:00Z",
                                                     "updatedAt": "2026-10-02T00:00:00Z", "author": {"login": f"a{n}"}, "labels": [],
                                                     "baseRefName": "master"} for n in numbers]))
        (g / "pr-bodies.json").write_text(json.dumps([{"number": n, "body": "", "reviewDecision": None} for n in numbers]))
        (g / "pr-extra.json").write_text(json.dumps({n: {"head": heads[n], "additions": 1, "deletions": 0, "files": 1, "author_role": "CONTRIBUTOR",
                                                         "comments": 0, "closing": [], "reviews": [], "ci": "SUCCESS"} for n in numbers}))
        (g / "mergers.json").write_text("{}")
        uf = {"pattern": r"^sql/[0-9_]+-to-[0-9_]+_upgrade\.sql$", "version_file": "version.php", "version_re": r"\$v_{part}\s*=\s*'(\d+)'",
              "parts": ["major", "minor", "patch"], "join": "_", "current": "-to-{version}_upgrade.sql", "glob": "sql/*-to-{version}_upgrade.sql"}
        profile = tmp / "profile.json"
        profile.write_text(json.dumps({**PROFILE, "version_claims": None, "mechanical": r"^sql/", "names": [], "name_paths": [], "upgrade_files": uf}))
        env = {**os.environ, "WIM_PROFILE": str(profile), "WIM_OFFLINE": "1"}
        work = tmp / "audit"
        r = subprocess.run([sys.executable, "-I", str(MOTION / "repo_audit.py"), str(repo), str(work), str(prlist)], capture_output=True, text=True, env=env)
        self.assertEqual(0, r.returncode, r.stderr[-1500:])
        r = subprocess.run([sys.executable, "-I", str(MOTION / "intent.py"), str(repo), str(work), str(g), str(MOTION / "page.html"), str(tmp / "p.html")],
                           capture_output=True, text=True, env=env)
        self.assertEqual(0, r.returncode, r.stderr[-1500:])
        m = json.loads((work / "map.json").read_text())
        self.assertEqual("sql/*-to-8_6_0_upgrade.sql", m["merge"]["stale_upgrade"]["current"])
        got = {n: (m["prs"][str(n)]["stale_sql"], m["prs"][str(n)]["stale_only"]) for n in numbers}
        self.assertEqual({71: ([old], True), 72: ([old], False), 73: ([], False)}, got)
        rows = {r["number"]: r for r in json.loads((tmp / "p-prs.json").read_text())["prs"]}
        self.assertEqual((False, ["upgrade SQL in an earlier release's file"]), (rows[71]["nothing_blocking"], rows[71]["blockers"]))
        self.assertEqual((True, True), (rows[72]["nothing_blocking"], rows[73]["nothing_blocking"]))


class Lenses(unittest.TestCase):
    """Owners from CODEOWNERS on each PR's own branch, one fix opened twice, status labels the build disagrees with,
    the data files, and pairs that share only mechanical files left unmerged when the profile says so."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tmp = Path(tempfile.mkdtemp())
        repo = cls.repo = tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "master")
        git(repo, "config", "user.email", "t@example.org")
        git(repo, "config", "user.name", "t")
        commit(repo, {".github/CODEOWNERS": "*.stub.php @stubs\n/ext/dom @branch-owner\n", "NEWS": "- one\n",
                      "ext/dom/dom.c": "1\n2\n3\n", "ext/dom/dom.stub.php": "<?php\n", "z.c": "z\n"}, "base")
        git(repo, "checkout", "-q", "-b", "R-1")
        commit(repo, {"y.c": "on the branch\n"}, "branch work")
        git(repo, "checkout", "-q", "master")
        git(repo, "merge", "-q", "--no-ff", "-m", "merge up", "R-1")
        # master's owners differ from the branch's, and its last rule for ext/dom wins over the earlier one
        commit(repo, {".github/CODEOWNERS": "*.stub.php @stubs\n/ext/dom @old-owner\n/ext/dom/ @master-owner\n"}, "owners change on master")
        for ref in ("master", "R-1"):
            git(repo, "update-ref", f"refs/remotes/origin/{ref}", git(repo, "rev-parse", ref))
        pr(repo, 61, "master", {"ext/dom/dom.c": "1 fixed\n2\n3\n", "NEWS": "- one\n- 61\n"})
        pr(repo, 62, "R-1", {"ext/dom/dom.c": "1 fixed\n2\n3\n", "NEWS": "- one\n- 62\n"})     # the same fix on the branch
        pr(repo, 63, "master", {"ext/dom/dom.stub.php": "<?php\nfunction f() {}\n", "NEWS": "- one\n- 63\n"})
        pr(repo, 64, "master", {"z.c": "z, 64\n", "lib/x.stub.php": "<?php\n"})
        cls.numbers, cls.bases = [61, 62, 63, 64], {61: "master", 62: "R-1", 63: "master", 64: "master"}
        cls.labels = {63: ["Waiting on Review"], 64: ["Waiting on Author"]}
        cls.prlist = tmp / "open-prs.txt"
        cls.prlist.write_text("".join(f"{n} {cls.bases[n]}\n" for n in cls.numbers))
        cls.profile = {**PROFILE, "version_claims": None, "mechanical": "^NEWS$", "names": [], "name_paths": [], "merge_up": ["R-1"],
                       "status_labels": {"Waiting on Review": "review", "Waiting on Author": "author"}}
        cls.map = cls.run_all("all", ci={63: "FAILURE"})

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def run_all(cls, pair_files, ci=None):
        tmp, repo = cls.tmp, cls.repo
        profile = tmp / f"profile-{pair_files}.json"
        profile.write_text(json.dumps({**cls.profile, "pair_files": pair_files}))
        env = {**os.environ, "WIM_PROFILE": str(profile), "WIM_OFFLINE": "1"}
        work, g = tmp / f"audit-{pair_files}", tmp / f"inputs-{pair_files}"
        r = subprocess.run([sys.executable, "-I", str(MOTION / "repo_audit.py"), str(repo), str(work), str(cls.prlist)], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr[-1500:]
        (g / "issues").mkdir(parents=True)
        heads = {n: git(repo, "rev-parse", f"refs/pr/{n}") for n in cls.numbers}
        (g / "pr-meta.json").write_text(json.dumps([{"number": n, "title": "Fix the DOM bug" if n in (61, 62) else f"PR {n}", "isDraft": False,
                                                     "createdAt": f"2026-0{n - 60}-01T00:00:00Z", "updatedAt": "2026-10-02T00:00:00Z",
                                                     "author": {"login": f"a{n}"}, "labels": [{"name": l} for l in cls.labels.get(n, [])],
                                                     "baseRefName": cls.bases[n]} for n in cls.numbers]))
        (g / "pr-bodies.json").write_text(json.dumps([{"number": n, "body": "", "reviewDecision": None} for n in cls.numbers]))
        (g / "pr-extra.json").write_text(json.dumps({n: {"head": heads[n], "additions": 1, "deletions": 0, "files": 1, "author_role": "CONTRIBUTOR",
                                                         "comments": 0, "closing": [], "reviews": [], "ci": (ci or {}).get(n, "SUCCESS")} for n in cls.numbers}))
        (g / "mergers.json").write_text("{}")
        cls.page = tmp / f"page-{pair_files}.html"
        r = subprocess.run([sys.executable, "-I", str(MOTION / "intent.py"), str(repo), str(work), str(g), str(MOTION / "page.html"), str(cls.page)],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr[-1500:]
        return json.loads((work / "map.json").read_text())

    def test_owners_from_each_prs_own_branch(self) -> None:
        prs = self.map["prs"]
        self.assertEqual(["@master-owner"], prs["61"]["owners"], "master's last rule for ext/dom wins")
        self.assertEqual(["@branch-owner"], prs["62"]["owners"], "the branch's CODEOWNERS, not master's")
        self.assertEqual(["@master-owner"], prs["63"]["owners"], "the later /ext/dom/ rule overrides *.stub.php for a stub there")
        self.assertEqual(["@stubs"], prs["64"]["owners"], "a pattern with no slash matches at any depth")

    def test_one_fix_opened_twice(self) -> None:
        twins = self.map["duplicates"]["same_title"]
        self.assertEqual([{"title": "Fix the DOM bug", "prs": [61, 62], "bases": ["master", "R-1"], "files": ["ext/dom/dom.c"]}], twins,
                         "NEWS, mechanical, doesn't make two PRs the same work")
        link = next(l for g in self.map["groups"] for l in g["links"] if (l["a"], l["b"]) == (61, 62))
        self.assertIn("same title", link["kinds"], "the two join a group: which one lands is a decision")

    def test_labels_the_build_disagrees_with(self) -> None:
        gaps = {g["n"]: g for g in self.map["labels_vs_build"]["gaps"]}
        self.assertEqual(["CI failing"], gaps[63]["but"], "labelled ready for review, with CI failing")
        self.assertEqual("waiting on its author", gaps[64]["says"], "labelled waiting on its author, with nothing blocking it")

    def test_the_data_files(self) -> None:
        ex = self.map["exports"]
        prs = json.loads((self.page.parent / ex["prs_json"]).read_text())
        self.assertEqual(("pr-toolbox motion prs 1", 4), (prs["schema"], len(prs["prs"])))
        row = {r["number"]: r for r in prs["prs"]}[63]
        self.assertEqual((False, ["CI failing"], ["@master-owner"]), (row["nothing_blocking"], row["blockers"], row["owners"]))
        self.assertEqual(["NEWS", "ext/dom/dom.stub.php"], row["changed_files"])
        import csv as csv_
        rows = list(csv_.DictReader((self.page.parent / ex["prs_csv"]).read_text().splitlines()))
        self.assertEqual(["61", "62", "63", "64"], [r["number"] for r in rows])
        self.assertEqual("@stubs", rows[3]["owners"])
        self.assertNotIn("changed_files", rows[0], "file lists stay in the JSON")
        groups = json.loads((self.page.parent / ex["groups_json"]).read_text())["groups"]
        self.assertEqual(self.map["groups"], groups)

    def test_pairs_sharing_only_mechanical_files(self) -> None:
        pairs = lambda mode: {k for k in json.loads((self.tmp / f"audit-{mode}" / "pairs.json").read_text())["pairs"]}
        self.run_all("code")
        both = {tuple(int(x.split(":")[0]) for x in k.split(",")) for k in pairs("all")}
        code = {tuple(int(x.split(":")[0]) for x in k.split(",")) for k in pairs("code")}
        self.assertIn((61, 63), both, "61 and 63 share only NEWS")
        self.assertNotIn((61, 63), code)
        self.assertIn((61, 62), code, "a pair sharing a code file is still merged")


class MergeUpFixture(unittest.TestCase):
    """A release branch merged up into master after every change, as php-src's PHP-8.x are: its PRs take their diff from
    their own branch, need a rebase only when they conflict with it, and carry a merge-up conflict when master differs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp())
        repo = cls.repo = cls.tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "master")
        git(repo, "config", "user.email", "t@example.org")
        git(repo, "config", "user.name", "t")
        root = commit(repo, {"x.c": "a\nb\nc\n", "y.c": "one\ntwo\n", "s.stub.php": "<?php\nfunction a(): void {}\n"}, "base")
        git(repo, "checkout", "-q", "-b", "R-1")
        old_branch = git(repo, "rev-parse", "HEAD")
        commit(repo, {"y.c": "one\nTWO on the branch\n"}, "fix on the branch")
        git(repo, "checkout", "-q", "master")
        git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'R-1'", "R-1")       # merged up
        commit(repo, {"x.c": "a\nB on master\nc\n"}, "master moves on")
        git(repo, "checkout", "-q", "R-1")
        branch_tip = commit(repo, {"w.c": "not merged up yet\n"}, "on the branch, not yet merged up")
        git(repo, "checkout", "-q", "master")
        git(repo, "update-ref", "refs/remotes/origin/master", git(repo, "rev-parse", "master"))
        git(repo, "update-ref", "refs/remotes/origin/R-1", branch_tip)
        pr(repo, 21, branch_tip, {"x.c": "a\nB fixed\nc\n"})                   # clean on R-1, conflicts on master
        pr(repo, 22, old_branch, {"y.c": "one\nTWO from 22\n"})               # conflicts with R-1 itself
        pr(repo, 23, "master", {"x.c": "a\nB on master\nC\n"})                # on master, clean
        pr(repo, 24, branch_tip, {"z.c": "new\n"})                             # forked after w.c: its diff is z.c only
        pr(repo, 25, "master", {"s.stub.php": "<?php\nfunction a(int $x): void {}\n"})   # edits a's signature
        pr(repo, 26, "master", {"s.stub.php": "<?php\nfunction a(string $y): void {}\n"})  # edits it too: adds nothing
        pr(repo, 27, "master", {"t.stub.php": "<?php\nfunction b(): void {}\n"})          # adds b
        pr(repo, 28, "master", {"u.stub.php": "<?php\nfunction b(): int {}\n"})           # adds b as well
        del root
        cls.prlist = cls.tmp / "open-prs.txt"
        cls.prlist.write_text("21 R-1\n22 R-1\n23 master\n24 R-1\n25 master\n26 master\n27 master\n28 master\n")
        cls.profile = cls.tmp / "profile.json"
        cls.names = [{"kind": "stub function", "where": {"suffix": ".stub.php"}, "pattern": r"^\s*function\s+(\w+)\s*\(", "added_only": True}]
        cls.profile.write_text(json.dumps({**PROFILE, "version_claims": None, "mechanical": "$^", "names": cls.names, "name_paths": ["*.stub.php"],
                                           "merge_up": ["R-1"]}))
        cls.work = cls.tmp / "audit"
        cls.findings = cls.audit(cls.profile, cls.work)

    @classmethod
    def audit(cls, profile: Path, work: Path) -> dict:
        r = subprocess.run([sys.executable, "-I", str(MOTION / "repo_audit.py"), str(cls.repo), str(work), str(cls.prlist)],
                           capture_output=True, text=True, env={**os.environ, "WIM_PROFILE": str(profile)})
        if r.returncode:
            raise AssertionError(r.stderr[-2000:])
        return json.loads((work / "findings.json").read_text())

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_name_is_shared_only_when_both_add_it(self) -> None:
        self.assertEqual([{"kind": "stub function", "name": "b", "prs": [27, 28]}], self.findings["shared_name"])
        # without added_only, the two signature edits read as both adding a()
        plain = self.tmp / "plain.json"
        plain.write_text(json.dumps({**json.loads(self.profile.read_text()), "names": [{**self.names[0], "added_only": False}]}))
        shared = {x["name"]: x["prs"] for x in self.audit(plain, self.tmp / "plain")["shared_name"]}
        self.assertEqual({"a": [25, 26], "b": [27, 28]}, shared)

    def test_its_diff_is_its_own_change(self) -> None:
        prs = json.loads((self.work / "prs.json").read_text())
        self.assertEqual(["z.c"], sorted(prs["24"]["files"]), "not the branch's commit master doesn't have yet")

    def test_onto_its_branch_then_up_the_chain(self) -> None:
        up = self.findings["merge_up"]
        self.assertEqual([{"branch": "R-1", "conflicts": []}, {"branch": "master", "conflicts": ["x.c"]}], up["21"]["path"])
        self.assertEqual(["y.c"], up["22"]["path"][0]["conflicts"])
        self.assertNotIn("23", up, "a PR on master has no chain")
        self.assertEqual([22], self.findings["summary"]["merge_up"]["conflict_with_own_base"])
        self.assertEqual({"R-1": 1}, self.findings["summary"]["merge_up"]["pending_into_base"])

    def test_rebase_means_its_own_branch(self) -> None:
        g = self.tmp / "inputs"
        (g / "issues").mkdir(parents=True, exist_ok=True)
        numbers, bases = [21, 22, 23, 24, 25, 26, 27, 28], {21: "R-1", 22: "R-1", 23: "master", 24: "R-1", 25: "master", 26: "master", 27: "master", 28: "master"}
        heads = {n: git(self.repo, "rev-parse", f"refs/pr/{n}") for n in numbers}
        (g / "pr-meta.json").write_text(json.dumps([{"number": n, "title": f"PR {n}", "isDraft": False, "createdAt": "2026-10-01T00:00:00Z",
                                                     "updatedAt": "2026-10-02T00:00:00Z", "author": {"login": f"a{n}"}, "labels": [],
                                                     "baseRefName": bases[n]} for n in numbers]))
        (g / "pr-bodies.json").write_text(json.dumps([{"number": n, "body": "", "reviewDecision": None} for n in numbers]))
        (g / "pr-extra.json").write_text(json.dumps({n: {"head": heads[n], "additions": 1, "deletions": 0, "files": 1, "author_role": "CONTRIBUTOR",
                                                         "comments": 0, "closing": [], "reviews": [], "ci": "SUCCESS"} for n in numbers}))
        (g / "mergers.json").write_text("{}")
        r = subprocess.run([sys.executable, "-I", str(MOTION / "intent.py"), str(self.repo), str(self.work), str(g),
                            str(MOTION / "page.html"), str(self.tmp / "page.html")],
                           capture_output=True, text=True, env={**os.environ, "WIM_PROFILE": str(self.profile), "WIM_OFFLINE": "1"})
        self.assertEqual(0, r.returncode, r.stderr[-1500:])
        out = json.loads((self.work / "map.json").read_text())["prs"]
        self.assertEqual((False, True, False), (out["21"]["rebase"], out["22"]["rebase"], out["23"]["rebase"]))
        self.assertEqual([{"branch": "master", "conflicts": ["x.c"]}], out["21"]["merge_up"])
        self.assertEqual(("R-1", "master"), (out["21"]["base"], out["23"]["base"]))
        page = (self.tmp / "page.html").read_text()
        self.assertIn("merge onto their own branch", page)


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
        self.assertIn(f"{self.page.resolve().parent}:/page:ro", cmd, "the page's directory, read only: its data files sit beside it")
        self.assertIn(f"/page/{self.page.name}", cmd[-1])
        self.assertIn(cli.PLAYWRIGHT_IMAGE, cmd)


class ConformanceChecks(unittest.TestCase):
    def test_ci_and_names_checks_can_find_and_can_miss(self) -> None:
        from pr_toolbox.motion import conformance
        flows = {".github/workflows/fhir.yml": "run: ci/inferno/run.sh", ".github/workflows/lint.yml": "run: phpcs"}
        self.assertEqual(conformance.ci_files("ci/inferno", flows), [".github/workflows/fhir.yml"])
        self.assertEqual(conformance.ci_files("no-such-tool", flows), [])
        text = "const A = 'us-core-bmi';\nconst B = BASE . 'us-core-blood-pressure';\n'http://x/us-core-vital-signs'"
        self.assertEqual(conformance.names_found(["us-core-bmi", "us-core-blood-pressure", "us-core-vital-signs", "us-core-heart-rate"], text),
                         {"us-core-bmi", "us-core-blood-pressure", "us-core-vital-signs"})
        self.assertEqual(conformance.names_found(["us-core-blood"], text), set(), "a name is a whole token, not a prefix")

    def test_measured_controls_fail_when_a_check_cannot_tell(self) -> None:
        from pr_toolbox.motion import conformance
        conf = {"specs": [{"id": "api", "paths": ["a/"], "ci_re": "ci/inferno", "names": {"label": "profiles", "where": ["a/"], "names": ["p-one"]}}],
                "controls": [{"ci": "api", "expect": "found"}, {"ci_re": "nothing-like-this", "expect": "none"}, {"names": "api", "planted": "p-fake"}]}
        ok = conformance.check(conf, ["a/x"], {}, {"w.yml": "ci/inferno"}, "'p-one'")[0]
        self.assertEqual(ok, [])
        # no workflow runs the tool; the code search found nothing at all; a matcher that matches anything
        self.assertTrue(any("CI control api" in f for f in conformance.check(conf, ["a/x"], {}, {"w.yml": "phpcs"}, "'p-one'")[0]))
        self.assertTrue(any("names control" in f for f in conformance.check(conf, ["a/x"], {}, {"w.yml": "ci/inferno"}, "")[0]))
        self.assertTrue(any("planted name was found" in f for f in conformance.check(conf, ["a/x"], {}, {"w.yml": "ci/inferno"}, "'p-one' 'p-fake'")[0]))

    def test_known_gaps_take_their_state_from_github(self) -> None:
        from pr_toolbox.motion import conformance
        conf = {"specs": [{"id": "s", "paths": ["a/"], "known": [{"text": "t", "url": "https://github.com/o/r/pull/7", "kind": "fix open"},
                                                                {"text": "u", "url": "https://forum.example/t/1", "kind": "meeting"}]}]}
        specs, _ = conformance.build(conf, {}, set(), issue=lambda n: {"state": "closed", "pr": True})
        self.assertEqual([(k["n"], k["state"]) for k in specs[0]["known"]], [(7, "closed"), (None, None)])


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

    def test_openemr_preset_carries_the_conformance_lens(self) -> None:
        from pr_toolbox import common
        from pr_toolbox.motion import conformance
        cfg = common.load_config(Path("."), "openemr/openemr", preset="openemr")
        target = common.Target(Path("."), "upstream", "openemr/openemr", "master", cfg)
        conf = cli.resolve_profile(target, None)["conformance"]
        certified = [s["label"] for s in conf["specs"] if s.get("certified")]
        self.assertIn("(g)(10)", certified)
        self.assertNotIn("(b)(3)", certified, "OpenEMR 8 isn't certified for e-prescribing")
        self.assertTrue(all(s.get("mapping") == "provisional" for s in conf["specs"]), "until a maintainer confirms the paths")
        self.assertTrue(all(s.get("hti5") or s.get("deadline") for s in conf["specs"]), "each says what the pending rule does")
        self.assertTrue(conf["source"]["url"] and conf["rule"]["url"], "the criteria and the rule cite their sources")
        # the file-and-title controls pass against the preset's own paths; controls naming PRs need them open
        paths = [p for s in conf["specs"] for p in s.get("paths", [])]
        base = paths + [f for c in conf["controls"] for f in c.get("files", [])]
        names = [n for s in conf["specs"] for n in (s.get("names") or {}).get("names", [])]
        flows = {".github/workflows/inferno-test.yml": "    - name: Run Inferno\n      working-directory: ci/inferno",
                 ".github/workflows/dependabot-auto-merge.yml": "steps.metadata.outputs.directory == '/ci/inferno'"}
        failures, notes = conformance.check(conf, base, {}, flows, " ".join(f"'{n}'" for n in names))
        self.assertEqual(failures, [])
        self.assertTrue(all("no longer open" in n for n in notes))

    def test_openemr_preset_paths_are_real_files_on_master(self) -> None:
        """The build checks every path against the base branch; this checks the preset against a local OpenEMR
        clone before a build, so a typo can't hide a standard (a path that matches nothing reads as "no PR touches
        it"). Set PR_TOOLBOX_OPENEMR to the clone (default ~/dev/openemr) and PR_TOOLBOX_OPENEMR_REF to the ref."""
        from pr_toolbox import common
        from pr_toolbox.motion import conformance
        repo = Path(os.environ.get("PR_TOOLBOX_OPENEMR", str(Path.home() / "dev" / "openemr")))
        ref = os.environ.get("PR_TOOLBOX_OPENEMR_REF", "upstream/master")
        r = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref], capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout:
            self.skipTest(f"no OpenEMR clone with {ref} at {repo}")
        files = r.stdout.splitlines()
        self.assertGreater(len(files), 5000, "the clone's tree, not a stub")
        cfg = common.load_config(Path("."), "openemr/openemr", preset="openemr")
        target = common.Target(Path("."), "upstream", "openemr/openemr", "master", cfg)
        conf = cli.resolve_profile(target, None)["conformance"]
        # Only the path and file controls: CI, names and PR controls need the build's own inputs.
        file_controls = [c for c in conf["controls"] if not ({"pr", "ci", "ci_re", "names"} & set(c))]
        failures, _ = conformance.check({**conf, "controls": file_controls}, files, {}, {}, "")
        self.assertEqual(failures, [])
        missing = [f for c in file_controls for f in c.get("files", []) if f not in files]
        self.assertEqual(missing, [], "each control's files exist, so the control tests a real change")
        # The check can fail: a planted path that no file starts with is reported.
        planted = {**conf, "controls": [], "specs": conf["specs"] + [{"id": "planted", "paths": ["no/such/folder/"]}]}
        self.assertIn("planted: no file on the base branch starts with no/such/folder/", conformance.check(planted, files, {}, {}, "")[0])

    def test_php_src_preset_is_a_folder(self) -> None:
        from pr_toolbox import common
        self.assertEqual("php-src", common.PRESET_FOR_REPO["php/php-src"])
        cfg = common.load_config(Path("."), "php/php-src", preset="php-src")
        p = cli.resolve_profile(common.Target(Path("."), "origin", "php/php-src", "master", cfg), None)
        self.assertEqual("php-src Work in Motion", p["title"])
        self.assertEqual(["PHP-8.2", "PHP-8.3", "PHP-8.4", "PHP-8.5", "PHP-8.6"], p["merge_up"])
        self.assertEqual(12, len(p["streams"]["streams"]), "the areas, read from streams.json beside profile.json")
        self.assertEqual(7, len(p["controls"]["controls"]), "the controls, read from controls.json beside it")
        for path in ("NEWS", "ext/standard/basic_functions_arginfo.h", "Zend/zend_vm_execute.h"):
            self.assertRegex(path, p["mechanical"])
        self.assertNotRegex("Zend/zend_vm_def.h", p["mechanical"], "the VM's definitions are written by hand")
        names = {k["kind"]: re.compile(k["pattern"]) for k in p["names"]}
        self.assertEqual("211", names["opcode number"].search("ZEND_VM_COLD_CONST_HANDLER(211, ZEND_NULLABLE_CAST, CONST|TMP, ANY)").group(1))
        self.assertTrue(all(k.get("added_only") for k in p["names"]))

    def test_a_profile_folder_anywhere_finds_its_own_files(self) -> None:
        from pr_toolbox import common
        mine = Path(tempfile.mkdtemp())
        (mine / "profile.json").write_text(json.dumps({"motion": {"streams": "streams.json"}}))
        (mine / "streams.json").write_text(json.dumps({"general": [], "rules_package": None, "streams": [{"id": "x", "title": "X"}]}))
        cfg = common.load_config(Path("/nonexistent"), "a/b", path=str(mine))
        p = cli.resolve_profile(common.Target(Path("/nonexistent"), "origin", "a/b", "main", cfg), None)
        self.assertEqual(["x"], [s["id"] for s in p["streams"]["streams"]])

    def test_decidim_preset(self) -> None:
        from pr_toolbox import common
        self.assertEqual(common.PRESET_FOR_REPO["decidim/decidim"], "decidim")
        cfg = common.load_config(Path("."), "decidim/decidim", preset="decidim")
        p = cli.resolve_profile(common.Target(Path("."), "origin", "decidim/decidim", "develop", cfg), None)
        self.assertEqual(p["title"], "Decidim Work in Motion")
        # lint runs on every PR; a first-time contributor's fork PR carries only the title check until approved
        self.assertRegex("Lint code (bundle exec rubocop -P)", p["ci_required"])
        self.assertNotRegex("Check PR title", p["ci_required"])
        for path in ("Gemfile.lock", "decidim-generators/Gemfile.lock", "RELEASE_NOTES.md", "decidim-core/config/locales/ca.yml"):
            self.assertRegex(path, p["mechanical"])
        for path in ("decidim-core/config/locales/en.yml", "decidim-core/lib/decidim/core.rb", "Gemfile"):
            self.assertNotRegex(path, p["mechanical"], "en.yml is written by hand; only Crowdin's locales are mechanical")
        kinds = {k["kind"]: re.compile(k["pattern"]) for k in p["names"]}
        found = {
            "migration": "class AddFailedAttemptsToDecidimAuthorizations < ActiveRecord::Migration[7.2]",
            "table": "    create_table :decidim_ai_spam_detection_stats do |t|",
            "content block": 'Decidim.content_blocks.register(:homepage, :highlighted_assemblies) do |content_block|',
            "environment variable": 'config_accessor :x, default: Decidim::Env.new("DECIDIM_THROTTLING_PERIOD", "1").to_i',
            "Decidim setting": "  mattr_accessor :throttling_period, default: 1",
        }
        missed = {"migration": "  # see AddFoo < ActiveRecord::Migration", "environment variable": 'ENV.fetch("DECIDIM_THROTTLING_PERIOD")',
                  "Decidim setting": "  attr_accessor :throttling_period"}
        for kind, line in found.items():
            self.assertTrue(kinds[kind].findall(line), kind)
        for kind, line in missed.items():
            self.assertFalse(kinds[kind].findall(line), f"{kind}: {line}")

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

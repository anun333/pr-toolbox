# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests that need no OpenEMR checkout, stack or network: the pure functions, and the git-backed
checks against small throwaway repositories."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openemr_pr_tools import common, landing_order, pr_check, pr_health  # noqa: E402


def repo_with(files: dict[str, str]) -> Path:
    path = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", "-b", "master", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.org"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    commit(path, files, "base")
    return path


def commit(path: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", message], check=True)


CAPS = "<?php\nreturn ['all' => [\n    'argument.type.php' => 2,\n]];\n"
BASELINE = "<?php\n$ignoreErrors[] = [1];\n$ignoreErrors[] = [2];\n"


class CommonTest(unittest.TestCase):
    def test_a_version_conflict_is_only_the_version_files_and_upgrade_scripts(self):
        self.assertTrue(common.is_version_conflict(["version.php", "sql/database.sql", "sql/8_4_1-to-8_5_0_upgrade.sql"]))
        self.assertFalse(common.is_version_conflict(["version.php", "src/Services/TaskService.php"]))
        self.assertFalse(common.is_version_conflict([]))

    def test_merge_tree_names_are_kept_and_messages_dropped(self):
        lines = ["src/A.php", "src/A.php", "Auto-merging src/A.php", "CONFLICT (content): Merge conflict in src/A.php", ""]
        self.assertEqual(["src/A.php"], common.conflicted_files(lines))

    def test_caps_count_the_entries_of_each_capped_file(self):
        repo = repo_with({common.CAPS_FILE: CAPS, ".phpstan/baseline/argument.type.php": BASELINE})
        self.assertEqual([], common.caps_mismatches(repo, "HEAD"))
        commit(repo, {".phpstan/baseline/argument.type.php": "<?php\n$ignoreErrors[] = [1];\n"}, "drop one")
        self.assertEqual(["argument.type: cap 2, entries 1"], common.caps_mismatches(repo, "HEAD"))

    def test_a_tree_without_caps_has_none_off(self):
        self.assertEqual([], common.caps_mismatches(repo_with({"README": "x"}), "HEAD"))

    def test_merged_reports_conflicts_and_merges_cleanly_otherwise(self):
        repo = repo_with({"a.txt": "one\n", "b.txt": "one\n"})
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "left"], check=True)
        commit(repo, {"a.txt": "left\n"}, "left")
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "right", "master"], check=True)
        commit(repo, {"a.txt": "right\n"}, "right")
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "other", "master"], check=True)
        commit(repo, {"b.txt": "other\n"}, "other")
        self.assertEqual((None, ["a.txt"]), common.merged(repo, "left", "right"))
        merge, conflicts = common.merged(repo, "left", "other")
        self.assertIsNotNone(merge)
        self.assertEqual([], conflicts)


def entry(**over) -> dict:
    base = {"title": "t", "draft": False, "head": "h", "v_database": None, "conflicts_with_master": [], "conflicts": {},
            "clean_with_ours": {}, "caps": [], "has_line": True}
    base.update(over)
    base["key"] = landing_order.fact_key(base)
    return base


RESULT = {"checked": "2026-10-19T22:00:00+00:00", "master": "7846cf5c12aaaa", "author": "me", "open_prs": 3, "ours": {}, "v_database_claims": {}}


class LandingOrderTest(unittest.TestCase):
    def test_an_independent_pr(self):
        line = landing_order.sentence("1", entry(), RESULT, {})
        self.assertTrue(line.startswith("**Landing order:** independent. No open PR conflicts with it."))
        self.assertIn("master 7846cf5c12 ", line)

    def test_version_conflicts_are_grouped_and_others_named(self):
        e = entry(v_database=558, conflicts={"20": ["version.php", "sql/database.sql"], "30": ["src/Foo.php"]})
        line = landing_order.sentence("1", e, RESULT, {})
        self.assertIn("takes `v_database` 558. It conflicts there and in `version.php` with #20", line)
        self.assertIn("It conflicts with #30 in `src/Foo.php`.", line)
        self.assertNotIn("independent", line)

    def test_a_stack_does_not_count_as_a_conflict_on_either_side(self):
        notes = {"9": {"carries": [1], "carries_text": "It carries copies of #1's commits."}}
        base_line = landing_order.sentence("1", entry(conflicts={"9": ["src/A.php"]}), RESULT, notes)
        self.assertIn("independent", base_line)
        draft_line = landing_order.sentence("9", entry(conflicts={"1": ["src/A.php"]}), RESULT, notes)
        self.assertIn("It carries copies of #1's commits.", draft_line)
        self.assertNotIn("It conflicts with #1", draft_line)

    def test_caps_and_clean_shares_are_mentioned(self):
        line = landing_order.sentence("1", entry(caps=[5], clean_with_ours={"6": ["src/B.php"]}), RESULT, {})
        self.assertIn("With #5 it would break an exact PHPStan cap", line)
        self.assertIn("It shares `src/B.php` with #6", line)

    def test_the_fact_key_ignores_the_title_and_the_check_time(self):
        self.assertEqual(entry(title="a")["key"], entry(title="b")["key"])
        self.assertNotEqual(entry()["key"], entry(conflicts={"2": ["x"]})["key"])

    def test_changes_are_reported_against_a_previous_check_only(self):
        now = dict(RESULT, ours={"1": entry(conflicts={"2": ["x"]}), "3": entry()})
        self.assertEqual([], landing_order.changes({}, now))
        before = dict(RESULT, ours={"1": entry()})
        self.assertEqual(["1", "3"], landing_order.changes(before, now))
        self.assertEqual("now conflicts with #2", landing_order.describe_change("1", before, now))
        self.assertEqual("new PR, no facts before", landing_order.describe_change("3", before, now))


def node(**over) -> dict:
    base = {"number": 1, "title": "t", "isDraft": False, "mergeable": "MERGEABLE", "reviewDecision": None, "headRefOid": "h", "url": "u",
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": []}}}}]}, "reviewThreads": {"nodes": []}}
    base.update(over)
    return base


def checks(*runs) -> dict:
    return {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [{"name": n, "conclusion": c, "status": "COMPLETED"} for n, c in runs]}}}}]}


def thread(who: str, resolved=False, outdated=False) -> dict:
    return {"isResolved": resolved, "isOutdated": outdated, "comments": {"nodes": [{"author": {"login": who}, "url": f"url-{who}"}]}}


IGNORE = re.compile(pr_health.DEFAULT_IGNORE)


class PrHealthTest(unittest.TestCase):
    def test_ci_leaves_out_codecov_and_the_summary_job(self):
        pr = pr_health.summarize(node(commits=checks(("codecov/patch", "FAILURE"), ("All Checks Passed", "FAILURE"), ("phpstan", "SUCCESS"))), "me", IGNORE)
        self.assertEqual(("passing", []), (pr["ci"], pr["failing"]))
        pr = pr_health.summarize(node(commits=checks(("phpstan", "FAILURE"), ("e2e", "CANCELLED"))), "me", IGNORE)
        self.assertEqual(("failing", ["phpstan"], 1), (pr["ci"], pr["failing"], pr["cancelled"]))

    def test_threads_wait_when_the_last_word_is_someone_elses_and_open(self):
        threads = {"nodes": [thread("reviewer"), thread("coderabbitai[bot]"), thread("me"), thread("old", resolved=True), thread("x", outdated=True)]}
        pr = pr_health.summarize(node(reviewThreads=threads), "me", IGNORE)
        self.assertEqual((["url-reviewer"], ["url-coderabbitai[bot]"]), (pr["waiting_people"], pr["waiting_bots"]))

    def test_events_follow_what_changed(self):
        before = pr_health.summarize(node(), "me", IGNORE)
        after = pr_health.summarize(node(mergeable="CONFLICTING", reviewDecision="APPROVED", commits=checks(("phpstan", "FAILURE")),
                                         reviewThreads={"nodes": [thread("reviewer")]}), "me", IGNORE)
        kinds = [e["kind"] for e in pr_health.events({"prs": {"1": before}}, {"1": after}, {})]
        self.assertEqual(["ci", "conflict", "review", "decision"], kinds)
        self.assertEqual([], pr_health.events({"prs": {"1": after}}, {"1": after}, {}))

    def test_an_unknown_merge_state_keeps_the_last_known_one(self):
        before = {"prs": {"1": pr_health.summarize(node(mergeable="CONFLICTING"), "me", IGNORE)}}
        now = {"1": pr_health.summarize(node(mergeable="UNKNOWN"), "me", IGNORE)}
        pr_health.carry_mergeable(before, now)
        self.assertEqual("CONFLICTING", now["1"]["mergeable"])

    def test_a_merged_dependency_says_to_rebase_and_mark_ready(self):
        dep = pr_health.summarize(node(number=1), "me", IGNORE)
        draft = pr_health.summarize(node(number=2, isDraft=True), "me", IGNORE)
        notes = {"2": {"depends_on": [1]}}
        with mock.patch.object(pr_health, "closed_state", return_value="merged by maintainer"):
            found = pr_health.events({"prs": {"1": dep, "2": draft}}, {"2": draft}, notes)
        self.assertEqual([("1", "closed", "merged by maintainer"), ("2", "dependency", "dependency merged: #1; rebase it and mark it ready")],
                         [(e["pr"], e["kind"], e["text"]) for e in found])

    def test_a_pr_missing_from_search_but_open_is_no_event(self):
        pr = pr_health.summarize(node(), "me", IGNORE)
        with mock.patch.object(pr_health, "closed_state", return_value="open"):
            self.assertEqual([], pr_health.events({"prs": {"1": pr}}, {}, {}))


class PrCheckTest(unittest.TestCase):
    def check_for(self, repo: Path, **over) -> pr_check.Check:
        args = argparse.Namespace(worktree=str(repo), container="none", app_dir="/app", base="master", workers=1, secrets_env=None,
                                  public_keys=None, forbid_trailer=None, require_trailer=None)
        for k, v in over.items():
            setattr(args, k, v)
        return pr_check.Check(args)

    def branch(self, files: dict[str, str], base_files: dict[str, str] | None = None, message: str = "change") -> Path:
        repo = repo_with(base_files or {"version.php": "<?php\n$v_database = 554;\n", "sql/database.sql": "-- v_database: 554\n"})
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "work"], check=True)
        commit(repo, files, message)
        return repo

    def test_sql_needs_a_higher_matching_v_database_and_balanced_blocks(self):
        repo = self.branch({"sql/8_4_1-to-8_5_0_upgrade.sql": "#IfNotTable t\nCREATE TABLE t (id int);\n"})
        status, detail = self.check_for(repo).upgrade_sql()
        self.assertEqual("fail", status)
        self.assertIn("1 #If blocks, 0 #EndIf", detail)
        self.assertIn("not above master's 554", detail)
        repo = self.branch({"sql/8_4_1-to-8_5_0_upgrade.sql": "#IfNotTable t\nCREATE TABLE t (id int);\n#EndIf\n",
                            "version.php": "<?php\n$v_database = 555;\n", "sql/database.sql": "-- v_database: 555\n"})
        self.assertEqual("pass", self.check_for(repo).upgrade_sql()[0])

    def test_secrets_in_the_diff_are_named_by_key(self):
        repo = self.branch({"notes.txt": "token s3cr3tvalue here\nhandle public-name\n"})
        env = Path(tempfile.mkdtemp()) / ".env"
        env.write_text("API_KEY=s3cr3tvalue\nUSER=public-name\nSHORT=abc\n")
        status, detail = self.check_for(repo, secrets_env=str(env), public_keys="USER").secrets()
        self.assertEqual(("fail", "values of API_KEY appear in the diff"), (status, detail))

    def test_trailers_are_required_and_forbidden_by_pattern(self):
        repo = self.branch({"a.txt": "x"}, message="fix: a\n\nCo-Authored-By: Someone <s@example.org>")
        status, detail = self.check_for(repo, forbid_trailer=["^Co-Authored-By:"], require_trailer=["^Assisted-by:"]).trailers()
        self.assertEqual("fail", status)
        self.assertIn("has /^Co-Authored-By:/", detail)
        self.assertIn("lacks /^Assisted-by:/", detail)


if __name__ == "__main__":
    unittest.main()

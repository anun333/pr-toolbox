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

from pr_toolbox import common, landing_order, pr_check, pr_health, trailers  # noqa: E402


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
OPENEMR = common.load_config(Path("."), "openemr/openemr", preset="openemr")
CAPS_RULE, VERSION_RULE = OPENEMR["exact_caps"], OPENEMR["version_claims"]
BASELINE = "<?php\n$ignoreErrors[] = [1];\n$ignoreErrors[] = [2];\n"


class CommonTest(unittest.TestCase):
    def test_a_version_conflict_is_only_the_files_that_go_with_the_number(self):
        self.assertTrue(common.is_version_conflict(["version.php", "sql/database.sql", "sql/8_4_1-to-8_5_0_upgrade.sql"], VERSION_RULE))
        self.assertFalse(common.is_version_conflict(["version.php", "src/Services/TaskService.php"], VERSION_RULE))
        self.assertFalse(common.is_version_conflict([], VERSION_RULE))
        self.assertFalse(common.is_version_conflict(["version.php"], None), "no rule, no version conflicts")

    def test_the_github_repository_comes_from_the_remote(self):
        self.assertEqual("openemr/openemr", common.github_of("https://github.com/openemr/openemr.git"))
        self.assertEqual("me/tool", common.github_of("git@github.com:me/tool.git"))
        self.assertEqual("me/tool", common.github_of("https://github.com/me/tool/"))
        self.assertIsNone(common.github_of("https://gitlab.com/me/tool.git"))

    def test_a_profile_is_chosen_by_file_then_preset_then_repository(self):
        repo = repo_with({"README": "x"})
        self.assertEqual("openemr", common.load_config(repo, "OpenEMR/openemr").get("name"))
        self.assertEqual({}, common.load_config(repo, "someone/else"))
        (repo / common.CONFIG_NAME).write_text('{"name": "mine"}')
        self.assertEqual("mine", common.load_config(repo, "openemr/openemr").get("name"))
        self.assertEqual("openemr", common.load_config(repo, "someone/else", preset="openemr").get("name"))
        (repo / common.CONFIG_NAME).unlink()
        (repo / ".pr-tools.json").write_text('{"name": "older"}')
        self.assertEqual("older", common.load_config(repo, "openemr/openemr").get("name"))

    def test_a_profile_in_a_folder_of_its_own(self):
        repo = repo_with({"README": "x"})
        php = common.load_config(repo, "php/php-src")
        self.assertEqual(("php-src", str(common.PRESETS / "php-src")), (php.get("name"), php.get("_dir")), "picked for php/php-src, from presets/php-src/")
        self.assertEqual(str(common.PRESETS), common.load_config(repo, "openemr/openemr")["_dir"], "a flat preset's folder is presets/")
        mine = Path(tempfile.mkdtemp()) / "profile"
        mine.mkdir()
        (mine / "profile.json").write_text('{"name": "mine"}')
        self.assertEqual(("mine", str(mine.resolve())), tuple(common.load_config(repo, "x/y", path=str(mine)).get(k) for k in ("name", "_dir")),
                         "--config takes a folder holding profile.json")

    def test_the_old_package_name_gives_the_same_modules(self):
        import openemr_pr_tools
        from openemr_pr_tools import common as old_common, landing_order as old_landing
        self.assertIs(common, old_common)
        self.assertIs(landing_order, old_landing)
        self.assertEqual(openemr_pr_tools.__version__, sys.modules["pr_toolbox"].__version__)

    def test_patterns_match_paths(self):
        self.assertTrue(common.matches("tests/Tests/Isolated/A/BTest.php", "tests/Tests/Isolated/*Test.php"))
        self.assertTrue(common.matches("src/x.php", ["*.js", "*.php"]))
        self.assertFalse(common.matches("src/x.php", None))

    def test_merge_tree_names_are_kept_and_messages_dropped(self):
        lines = ["src/A.php", "src/A.php", "Auto-merging src/A.php", "CONFLICT (content): Merge conflict in src/A.php", ""]
        self.assertEqual(["src/A.php"], common.conflicted_files(lines))

    def test_caps_count_the_entries_of_each_capped_file(self):
        repo = repo_with({CAPS_RULE["file"]: CAPS, ".phpstan/baseline/argument.type.php": BASELINE})
        self.assertEqual([], common.caps_mismatches(repo, "HEAD", CAPS_RULE))
        commit(repo, {".phpstan/baseline/argument.type.php": "<?php\n$ignoreErrors[] = [1];\n"}, "drop one")
        self.assertEqual(["argument.type: cap 2, entries 1"], common.caps_mismatches(repo, "HEAD", CAPS_RULE))
        self.assertEqual([], common.caps_mismatches(repo, "HEAD", None), "no rule, nothing pinned")

    def test_a_tree_without_caps_has_none_off(self):
        self.assertEqual([], common.caps_mismatches(repo_with({"README": "x"}), "HEAD", CAPS_RULE))

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


RESULT = {"checked": "2026-10-19T22:00:00+00:00", "master": "7846cf5c12aaaa", "base_branch": "master", "github": "openemr/openemr", "author": "me",
          "open_prs": 3, "ours": {}, "v_database_claims": {}, "version_noun": "`v_database`", "version_file": "version.php",
          "version_related": VERSION_RULE["related"], "caps_noun": "PHPStan cap"}


class LandingOrderTest(unittest.TestCase):
    def test_an_independent_pr(self):
        line = landing_order.sentence("1", entry(), RESULT, {})
        self.assertTrue(line.startswith("**Landing order:** independent. No open PR conflicts with it."))
        self.assertIn("against master 7846cf5c12 and every open PR", line)

    def test_a_pr_that_no_longer_merges_onto_the_base_is_not_called_independent(self):
        line = landing_order.sentence("1", entry(conflicts_with_master=["version.php", "sql/8_4_1-to-8_5_0_upgrade.sql"]), RESULT, {})
        self.assertNotIn("independent", line)
        self.assertIn("It no longer merges onto master (in `version.php`, `sql/8_4_1-to-8_5_0_upgrade.sql`), so it needs a rebase", line)

    def test_the_same_number_without_a_text_conflict_is_named(self):
        e = entry(v_database=556, same_claim=[14806])
        self.assertIn("It takes `v_database` 556, and #14806 takes the same number, which merges cleanly as text, so whichever lands second needs the next one",
                      landing_order.sentence("1", e, RESULT, {}))
        e = entry(v_database=556, same_claim=[14806], conflicts={"20": ["version.php", "sql/database.sql"]})
        self.assertIn("it conflicts with #20 only over that number (in `version.php` and the files that go with it), and #14806 takes the same number",
                      landing_order.sentence("1", e, RESULT, {}))
        self.assertNotEqual(entry(v_database=556)["key"], e["key"], "a new sharer changes the facts")
        self.assertEqual(entry(v_database=556)["key"], entry(v_database=556, same_claim=[])["key"], "keys of PRs without one don't move")

    def test_a_rewrite_refuses_a_pr_that_moved_since_the_check(self):
        result = dict(RESULT, ours={"1": entry(head="a" * 40)})
        with mock.patch.object(landing_order, "gh", return_value='{"head": {"sha": "' + "b" * 40 + '"}, "body": "x"}') as gh:
            with self.assertRaises(common.ToolError) as raised:
                landing_order.rewrite(["1"], result, {}, Path(tempfile.mkdtemp()), "openemr/openemr")
        self.assertIn("#1 moved since the last check (aaaaaaaaaa then, bbbbbbbbbb now)", str(raised.exception))
        self.assertEqual(1, gh.call_count, "nothing was written")

    def test_version_conflicts_are_grouped_and_others_named(self):
        e = entry(v_database=558, conflicts={"20": ["version.php", "sql/database.sql"], "30": ["src/Foo.php"]})
        line = landing_order.sentence("1", e, RESULT, {})
        self.assertIn("It takes `v_database` 558, and it conflicts with #20 only over that number (in `version.php`", line)
        self.assertIn("It conflicts with #30 in `src/Foo.php`.", line)
        self.assertNotIn("independent", line)

    def test_a_stack_does_not_count_as_a_conflict_on_either_side(self):
        notes = {"9": {"carries": [1], "carries_text": "It carries copies of #1's commits."}}
        base_line = landing_order.sentence("1", entry(conflicts={"9": ["src/A.php"]}), RESULT, notes)
        self.assertIn("independent", base_line)
        draft_line = landing_order.sentence("9", entry(conflicts={"1": ["src/A.php"]}), RESULT, notes)
        self.assertIn("It carries copies of #1's commits.", draft_line)
        self.assertNotIn("It conflicts with #1", draft_line)

    def test_without_a_version_rule_every_conflict_is_named(self):
        generic = dict(RESULT, version_file="", version_related=[])
        line = landing_order.sentence("1", entry(conflicts={"20": ["version.php"]}), generic, {})
        self.assertIn("It conflicts with #20 in `version.php`.", line)

    def test_caps_and_clean_shares_are_mentioned(self):
        line = landing_order.sentence("1", entry(caps=[5], clean_with_ours={"6": ["src/B.php"]}), RESULT, {})
        self.assertIn("With #5 it would break an exact PHPStan cap", line)
        self.assertIn("It shares `src/B.php` with #6", line)

    def stale_partners(self, count: int):
        """A real repository: our PR 1 changes a.txt; partners 2.. share it and also conflict with master."""
        repo = repo_with({"a.txt": "one\n", "b.txt": "one\n"})
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
        git("checkout", "-q", "-b", "ours", "master")
        commit(repo, {"a.txt": "ours\n"}, "ours")
        for k in range(2, 2 + count):
            git("checkout", "-q", "-b", f"partner{k}", "master")
            commit(repo, {"a.txt": f"one\n\npartner {k}\n", "b.txt": f"partner {k}\n"}, f"partner {k}")
        git("checkout", "-q", "master")
        commit(repo, {"b.txt": "master moved\n"}, "master moves")
        git("update-ref", "refs/remotes/upstream/master", "master")
        git("update-ref", "refs/remotes/upstream/pr/1", "ours")
        prs = [{"number": 1, "title": "ours", "author": "me", "isDraft": False, "headRefOid": "h1", "body": "", "paths": {"a.txt"}}]
        for k in range(2, 2 + count):
            git("update-ref", f"refs/remotes/upstream/pr/{k}", f"partner{k}")
            prs.append({"number": k, "title": f"partner {k}", "author": "them", "isDraft": False, "headRefOid": f"h{k}", "body": "",
                        "paths": {"a.txt", "b.txt"}})
        return common.Target(repo=repo, remote="upstream", github="o/r", base_branch="master", config={}), prs

    def landing(self, target, prs, previous=None):
        with mock.patch.object(landing_order, "open_prs", return_value=prs), mock.patch.object(landing_order, "fetch_prs"):
            return landing_order.check(target, "me", previous)

    def test_a_partner_that_conflicted_before_and_stopped_merging_is_named_unchecked(self):
        # #14669 and #14808 (2026-10-26): once #14808 stopped merging onto master, the pair dropped out of the
        # check and #14669's line read as if the conflict had gone.
        target, prs = self.stale_partners(1)
        previous = dict(RESULT, ours={"1": entry(conflicts={"2": ["a.txt"]})})
        result = self.landing(target, prs, previous)
        facts = result["ours"]["1"]
        self.assertEqual({"2": ["b.txt"]}, facts.get("unchecked"), "the partner is kept, with why it couldn't be checked")
        line = landing_order.sentence("1", facts, dict(RESULT, master=result["master"]), {})
        self.assertNotIn("independent", line)
        self.assertIn("It couldn't be test-merged with #2, which no longer merges onto master itself, so that pair is unchecked until #2 is rebased.", line)
        self.assertNotEqual(entry()["key"], facts["key"], "an unchecked partner changes the facts")
        self.assertEqual(entry()["key"], entry(unchecked={})["key"], "keys of PRs without one don't move")
        self.assertEqual("can't be checked against #2 (no longer merges onto the base)",
                         landing_order.describe_change("1", previous, dict(RESULT, ours={"1": facts})))
        # while it stays stale, the next check keeps naming it (the facts carry the 0.8.3 rule)
        again = self.landing(target, prs, result)
        self.assertEqual({"2": ["b.txt"]}, again["ours"]["1"].get("unchecked"))
        self.assertEqual([], landing_order.changes(result, again), "nothing changed, so no line is rewritten")

    def test_partners_that_were_already_stale_are_never_listed(self):
        # 0.8.2 listed every stale partner sharing a file; on openemr/openemr that was a dozen PRs per line.
        target, prs = self.stale_partners(5)
        for previous in (None, dict(RESULT, ours={"1": entry()})):
            facts = self.landing(target, prs, previous)["ours"]["1"]
            self.assertNotIn("unchecked", facts)
            self.assertEqual(entry()["key"], facts["key"], "the same facts as 0.8.1")
            self.assertIn("independent", landing_order.sentence("1", facts, RESULT, {}))
        # 0.8.2's own facts listed them all as unchecked; those lists aren't carried forward
        polluted = dict(RESULT, ours={"1": entry(unchecked={str(k): ["b.txt"] for k in range(2, 7)})})
        self.assertNotIn("unchecked", self.landing(target, prs, polluted)["ours"]["1"])

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


IGNORE = re.compile(OPENEMR["health_ignore"])


class PrHealthTest(unittest.TestCase):
    def test_ci_leaves_out_codecov_and_the_summary_job(self):
        pr = pr_health.summarize(node(commits=checks(("codecov/patch", "FAILURE"), ("All Checks Passed", "FAILURE"), ("phpstan", "SUCCESS"))), "me", IGNORE)
        self.assertEqual(("passing", []), (pr["ci"], pr["failing"]))
        pr = pr_health.summarize(node(commits=checks(("phpstan", "FAILURE"), ("e2e", "CANCELLED"))), "me", IGNORE)
        self.assertEqual(("failing", ["phpstan"], 1), (pr["ci"], pr["failing"], pr["cancelled"]))

    def test_without_a_profile_only_codecov_is_left_out(self):
        pr = pr_health.summarize(node(commits=checks(("codecov/patch", "FAILURE"), ("All Checks Passed", "FAILURE"))), "me", re.compile(pr_health.DEFAULT_IGNORE))
        self.assertEqual(["All Checks Passed"], pr["failing"])

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
    def check_for(self, repo: Path, config: dict | None = None, **over) -> pr_check.Check:
        args = argparse.Namespace(container="none", app_dir=None, workers=1, secrets_env=None, public_keys=None,
                                  forbid_trailer=None, require_trailer=None)
        for k, v in over.items():
            setattr(args, k, v)
        subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "master"], check=True)
        target = common.Target(repo, "origin", "me/tool", "master", OPENEMR if config is None else config)
        return pr_check.Check(args, target)

    def branch(self, files: dict[str, str], base_files: dict[str, str] | None = None, message: str = "change") -> Path:
        repo = repo_with(base_files or {"version.php": "<?php\n$v_database = 554;\n", "sql/database.sql": "-- v_database: 554\n"})
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "work"], check=True)
        commit(repo, files, message)
        return repo

    def test_sql_needs_a_higher_matching_v_database_and_balanced_blocks(self):
        repo = self.branch({"sql/8_4_1-to-8_5_0_upgrade.sql": "#IfNotTable t\nCREATE TABLE t (id int);\n"})
        status, detail = self.check_for(repo).version()
        self.assertEqual("fail", status)
        self.assertIn("1 blocks opened, 0 closed", detail)
        self.assertIn("is 554, not above the base's 554", detail)
        repo = self.branch({"sql/8_4_1-to-8_5_0_upgrade.sql": "#IfNotTable t\nCREATE TABLE t (id int);\n#EndIf\n",
                            "version.php": "<?php\n$v_database = 555;\n", "sql/database.sql": "-- v_database: 555\n"})
        self.assertEqual("pass", self.check_for(repo).version()[0])
        self.assertEqual("skip", self.check_for(repo, config={}).version()[0], "no profile, no version rule")

    def test_a_claim_clashes_with_other_prs_but_not_the_branchs_own_or_one_it_is_built_on(self):
        repo = self.branch({"version.php": "<?php\n$v_database = 556;\n", "sql/database.sql": "-- v_database: 556\n"})
        pushed = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        commit(repo, {"a.php": "<?php\n"}, "a commit on top, not pushed yet")
        prs = [{"number": 1, "author": "me", "headRefOid": pushed, "paths": ["version.php"]},           # this branch's own PR
               {"number": 2, "author": "them", "headRefOid": "f" * 40, "paths": ["version.php"]}]       # another PR, not in our history
        with mock.patch.object(pr_check, "open_prs", return_value=prs), \
                mock.patch.object(pr_check, "gh", return_value="<?php\n$v_database = 556;\n"):
            self.assertEqual(("fail", "`v_database` 556 is also claimed by #2 (them)"), self.check_for(repo).version_claims())
        with mock.patch.object(pr_check, "open_prs", return_value=prs[:1]), \
                mock.patch.object(pr_check, "gh", return_value="<?php\n$v_database = 556;\n"):
            self.assertEqual("pass", self.check_for(repo).version_claims()[0], "its own PR's earlier head is no clash")
        # rebased: the pushed head is no longer in HEAD's history, but the remote-tracking branch still names it
        subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/fork/work", pushed], check=True)
        subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard", "master"], check=True)
        commit(repo, {"version.php": "<?php\n$v_database = 556;\n", "sql/database.sql": "-- v_database: 556\n", "b.php": "<?php\n"}, "rebased")
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "stacked", pushed], check=True)
        commit(repo, {"c.php": "<?php\n"}, "another PR, built on the branch as it was pushed")
        stacked = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "work"], check=True)
        own_and_stacked = [{"number": 1, "author": "me", "headRefOid": pushed, "paths": ["version.php"]},
                           {"number": 3, "author": "me", "headRefOid": stacked, "paths": ["version.php"]}]
        with mock.patch.object(pr_check, "open_prs", return_value=own_and_stacked + prs[1:]), \
                mock.patch.object(pr_check, "gh", return_value="<?php\n$v_database = 556;\n"):
            self.assertEqual(("fail", "`v_database` 556 is also claimed by #2 (them)"), self.check_for(repo).version_claims(),
                             "after a rebase, its own PR and one built on it are no clash; another PR still is")

    def test_project_steps_run_on_the_host_with_their_files(self):
        repo = self.branch({"a.txt": "fine\n", "b.txt": "bad word\n", "c.md": "bad\n"})
        steps = {"check_steps": [
            {"name": "all-at-once", "files": "*.txt", "run": "! grep -l bad {files}"},
            {"name": "per-file", "each": "*.txt", "run": "! grep -q bad {file}"},
            {"name": "pattern", "files": "*.txt", "run": "cat {files}", "fail_pattern": "bad"},
            {"name": "nothing-matches", "files": "*.py", "run": "false"},
        ]}
        check = self.check_for(repo, config=steps)
        results = {name: run() for name, run in check.plan()}
        self.assertEqual("fail", results["all-at-once"][0])
        self.assertEqual("fail", results["per-file"][0])
        self.assertIn("a.txt:", results["per-file"][1])
        self.assertEqual("fail", results["pattern"][0])
        self.assertEqual("skip", results["nothing-matches"][0])
        self.assertEqual("pass", results["merges"][0])
        self.assertEqual(["merges", "all-at-once", "per-file", "pattern", "nothing-matches", "codespell", "caps", "version", "version-claims", "secrets", "trailers"],
                         [name for name, _ in check.plan()])

    def test_php_src_steps_check_stub_hashes_and_skip_tests_without_a_build(self):
        import hashlib
        php = common.load_config(Path("."), "php/php-src", preset="php-src")
        stub = "<?php\nfunction f(): void {}\n"
        header = f"/* This is a generated file, edit x.stub.php instead.\n * Stub hash: {hashlib.sha1(stub.encode()).hexdigest()} */\n"
        repo = self.branch({"ext/x/x.stub.php": stub, "ext/x/x_arginfo.h": header, "ext/x/tests/a.phpt": "--TEST--\n"}, base_files={"README": "x"})
        check = self.check_for(repo, config=php)
        steps = {s["name"]: s for s in php["check_steps"]}
        self.assertEqual(("pass", "1 files"), check.command_step(steps["generated headers"]))
        status, detail = check.command_step(steps["changed tests"])
        self.assertEqual("skip", status)
        self.assertIn("sapi/cli/php isn't in the checkout: build this checkout first", detail)
        # Control: the stub changes and its header doesn't
        commit(repo, {"ext/x/x.stub.php": stub + "function g(): void {}\n"}, "stub only")
        status, detail = self.check_for(repo, config=php).command_step(steps["generated headers"])
        self.assertEqual("fail", status)
        self.assertIn("run build/gen_stub.php ext/x/x.stub.php", detail)

    def test_a_step_whose_tool_is_missing_is_skipped_and_says_so(self):
        repo = self.branch({"a.js": "x;\n", "tools/lint": "#!/bin/sh\nexit 1\n"})
        (repo / "tools/lint").chmod(0o755)
        step = {"name": "lint", "files": "*.js", "needs": "node_modules/.bin/eslint", "needs_hint": "run npm ci", "run": "false"}
        check = self.check_for(repo, config={"check_steps": [step]})
        self.assertEqual(("skip", "node_modules/.bin/eslint isn't in the checkout: run npm ci"), check.command_step(step))
        # Control: when the tool is there, the step runs, and fails here.
        self.assertEqual("fail", check.command_step({**step, "needs": "tools/lint"})[0])

    def test_a_container_step_without_a_container_fails(self):
        repo = self.branch({"a.php": "<?php\n"})
        check = self.check_for(repo, config={"check_steps": [{"name": "lint", "where": "container", "each": "*.php", "run": "php -l {file}"}]})
        with self.assertRaises(RuntimeError):
            check.command_step(check.config["check_steps"][0])

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

    def test_without_flags_the_profiles_trailer_rules_are_forbidden(self):
        repo = self.branch({"a.txt": "x"}, message="fix: a\n\nCo-Authored-By: Claude <c@example.org>")
        profile = {"trailers": {"replace": [{"from": "^Co-Authored-By: Claude", "to": "Assisted-by: Claude Code"}]}}
        status, detail = self.check_for(repo, config=profile).trailers()
        self.assertEqual("fail", status)
        self.assertIn("pr-toolbox trailers", detail)
        self.assertEqual("skip", self.check_for(repo, config={}).trailers()[0])


CLAUDE = "Co-Authored-By: Claude Opus <noreply@example.org>"
SWAP = trailers.Rules.make([], [("^Co-Authored-By: Claude", "Assisted-by: Claude Code")])


def git_out(repo: Path, *args: str, **env: str) -> str:
    import os
    run = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env={**os.environ, **env})
    return run.stdout.strip()


def dated_commit(repo: Path, files: dict[str, str], message: str, when: str) -> str:
    for name, text in files.items():
        (repo / name).write_text(text)
    git_out(repo, "add", "-A")
    git_out(repo, "commit", "-q", "-m", message, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when,
            GIT_AUTHOR_NAME="Ann Author", GIT_AUTHOR_EMAIL="ann@example.org")
    return git_out(repo, "rev-parse", "HEAD")


class TrailersTest(unittest.TestCase):
    def test_messages(self):
        block = f"fix: a\n\nWhy it changed.\n\n{CLAUDE}\nSigned-off-by: A <a@example.org>\n"
        new, removed, added = trailers.rewrite_message(block, SWAP)
        self.assertEqual("fix: a\n\nWhy it changed.\n\nSigned-off-by: A <a@example.org>\nAssisted-by: Claude Code\n", new)
        self.assertEqual(([CLAUDE], ["Assisted-by: Claude Code"]), (removed, added))
        new, _, _ = trailers.rewrite_message(f"docs: a\n\n{CLAUDE}\n", SWAP)
        self.assertEqual("docs: a\n\nAssisted-by: Claude Code\n", new, "a subject alone is not a trailer block")
        new, _, added = trailers.rewrite_message(f"fix: a\n\nAssisted-by: Claude Code\n{CLAUDE}\n", SWAP)
        self.assertEqual(("fix: a\n\nAssisted-by: Claude Code\n", []), (new, added))
        prose = "fix: a\n\nWe thanked Co-Authored-By: Claude in prose.\n"
        self.assertEqual(prose, trailers.rewrite_message(prose, SWAP)[0], "only lines shaped like trailers match")
        squashed = f"feat: b\n\n* part one\n\n{CLAUDE}\n\n* part two\n\n{CLAUDE}\n"
        new, removed, added = trailers.rewrite_message(squashed, SWAP)
        self.assertEqual((2, 1), (len(removed), len(added)))
        self.assertEqual(1, new.count("Assisted-by: Claude Code"))
        dropped = trailers.Rules.make(["^Test-Mode:"], [])
        self.assertEqual(("fix: a\n", ["Test-Mode: full"], []), trailers.rewrite_message("fix: a\n\nTest-Mode: full\n", dropped))

    def branch(self) -> tuple[Path, str, str]:
        repo = repo_with({"a.txt": "base\n"})
        git_out(repo, "checkout", "-q", "-b", "work")
        dated_commit(repo, {"a.txt": "one\n"}, "feat: one", "1700000000 +0200")
        dated_commit(repo, {"b.txt": "two\n"}, f"fix: two\n\nBody.\n\n{CLAUDE}", "1700000100 -0500")
        git_out(repo, "checkout", "-q", "master")
        dated_commit(repo, {"m.txt": "master moved\n"}, "chore: master", "1700000200 +0000")
        git_out(repo, "checkout", "-q", "work")
        git_out(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'master' into work", "master",
                GIT_AUTHOR_DATE="1700000300 +0000", GIT_COMMITTER_DATE="1700000300 +0000")
        dated_commit(repo, {"c.txt": "three\n"}, f"docs: three\n\n{CLAUDE}", "1700000400 +0100")
        return repo, git_out(repo, "merge-base", "master", "work"), git_out(repo, "rev-parse", "work")

    def test_a_range_keeps_every_tree_author_and_date(self):
        repo, base, head = self.branch()
        mapping, shas, changed = trailers.rewrite_range(repo, base, head, SWAP)
        self.assertEqual(4, len(shas), "one, two, the merge, three")
        self.assertEqual(shas[0], mapping[shas[0]], "a commit before the first change keeps its sha")
        self.assertEqual(["fix: two", "docs: three"], [c["subject"] for c in changed])
        self.assertEqual([], trailers.verify(repo, base, mapping, shas, SWAP))
        new = mapping[head]
        fmt = "--format=%T %an <%ae> %ad %cn <%ce> %cd"
        self.assertEqual(git_out(repo, "log", "--date=raw", fmt, f"{base}..{head}"),
                         git_out(repo, "log", "--date=raw", fmt, f"{base}..{new}"))
        messages = git_out(repo, "log", "--format=%B", f"{base}..{new}")
        self.assertNotIn("Co-Authored-By", messages)
        self.assertEqual(2, messages.count("Assisted-by: Claude Code"))
        self.assertEqual(2, len(git_out(repo, "rev-list", "--parents", "-1", new + "^").split()) - 1, "the merge is still a merge")
        same, _, none = trailers.rewrite_range(repo, base, head, trailers.Rules.make(["^Nope:"], []))
        self.assertEqual((head, []), (same[head], none), "no match, no new commits")

    def test_a_tampered_rewrite_is_caught(self):
        repo, base, head = self.branch()
        mapping, shas, _ = trailers.rewrite_range(repo, base, head, SWAP)
        other = git_out(repo, "commit-tree", git_out(repo, "rev-parse", f"{base}^{{tree}}"), "-p", base, "-m", "x")
        mapping[shas[1]] = other
        self.assertTrue(trailers.verify(repo, base, mapping, shas, SWAP))

    def test_signed_commits_are_refused(self):
        repo = repo_with({"a.txt": "base\n"})
        base = git_out(repo, "rev-parse", "HEAD")
        raw = git_out(repo, "cat-file", "commit", base).split("\n\n", 1)[0]
        lines = raw.split("\n")
        obj = "\n".join([lines[0], f"parent {base}", *lines[1:]]) + "\ngpgsig -----BEGIN PGP SIGNATURE-----\n x\n -----END PGP SIGNATURE-----\n\nfix: a\n\n" + CLAUDE + "\n"
        sha = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"], input=obj,
                             capture_output=True, text=True, check=True).stdout.strip()
        with self.assertRaisesRegex(common.ToolError, "signed"):
            trailers.rewrite_range(repo, base, sha, SWAP)

    def remotes(self) -> tuple[Path, Path, Path, str]:
        """A clone with upstream (the project, holding refs/pull/7/head and /8/head) and origin (your fork)."""
        work, base, head = self.branch()
        upstream, fork = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        for bare in (upstream, fork):
            subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(bare)], check=True)
        git_out(work, "push", "-q", str(upstream), "master", "work:refs/pull/7/head", "work:refs/pull/8/head")
        git_out(work, "push", "-q", str(fork), "work")
        clone = Path(tempfile.mkdtemp()) / "clone"
        subprocess.run(["git", "clone", "-q", "-o", "upstream", str(upstream), str(clone)], check=True)
        git_out(clone, "remote", "add", "origin", str(fork))
        return clone, upstream, fork, head

    def run_main(self, clone: Path, *argv: str) -> tuple[int, str]:
        import contextlib
        import io

        def info(github, number):
            return {"state": "open", "user": {"login": "me" if number == 7 else "someone"},
                    "head": {"ref": "work", "sha": head_of(clone, number), "repo": {"full_name": "me/tool"}}}

        def head_of(clone, number):
            return git_out(Path(self.upstream), "rev-parse", f"refs/pull/{number}/head")

        out = io.StringIO()
        with mock.patch.object(trailers, "pr_info", info), mock.patch.object(trailers, "current_login", return_value="me"), \
                contextlib.redirect_stdout(out):
            code = trailers.main(["--repo", str(clone), "--github-repo", "owner/tool", "--base-branch", "master",
                                  "--remote", "upstream", "--push-remote", "origin", *argv])
        return code, out.getvalue()

    def test_the_command_dry_runs_then_pushes_only_your_prs(self):
        clone, self.upstream, fork, head = self.remotes()
        swap = ["--replace", "^Co-Authored-By: Claude", "Assisted-by: Claude Code"]
        code, out = self.run_main(clone, "7", "8", *swap)
        self.assertEqual(0, code, out)
        self.assertIn("2 of 4 commits rewritten", out)
        self.assertIn("Dry run: nothing pushed", out)
        self.assertIn("#8: skipped, opened by someone, not you", out)
        self.assertEqual(head, git_out(fork, "rev-parse", "work"), "a dry run pushes nothing")
        code, out = self.run_main(clone, "7", "8", "--push", *swap)
        self.assertEqual(0, code, out)
        pushed = git_out(fork, "rev-parse", "work")
        self.assertNotEqual(head, pushed)
        self.assertEqual(git_out(fork, "rev-parse", f"{head}^{{tree}}"), git_out(fork, "rev-parse", f"{pushed}^{{tree}}"))
        self.assertNotIn("Co-Authored-By", git_out(fork, "log", "--format=%B", "work"))

    def test_the_lease_refuses_a_branch_that_moved(self):
        clone, self.upstream, fork, head = self.remotes()
        elsewhere = Path(tempfile.mkdtemp()) / "elsewhere"
        subprocess.run(["git", "clone", "-q", "-b", "work", str(fork), str(elsewhere)], check=True)
        git_out(elsewhere, "config", "user.email", "t@example.org")
        git_out(elsewhere, "config", "user.name", "t")
        git_out(elsewhere, "commit", "-q", "--allow-empty", "-m", "pushed from another machine")
        git_out(elsewhere, "push", "-q", "origin", "work")
        moved = git_out(fork, "rev-parse", "work")
        code, out = self.run_main(clone, "7", "--push", "--replace", "^Co-Authored-By: Claude", "Assisted-by: Claude Code")
        self.assertEqual(1, code, out)
        self.assertIn("push refused, nothing changed", out)
        self.assertEqual(moved, git_out(fork, "rev-parse", "work"))

    def test_the_profile_supplies_rules_when_no_flags_are_given(self):
        clone, self.upstream, fork, head = self.remotes()
        code, out = self.run_main(clone, "7")
        self.assertEqual(2, code, "no rules anywhere")
        (clone / ".pr-toolbox.json").write_text('{"trailers": {"remove": ["^Co-Authored-By: Claude"]}}')
        code, out = self.run_main(clone, "7")
        self.assertEqual(0, code, out)
        self.assertIn("     - " + CLAUDE, out)
        self.assertNotIn("     + ", out)


if __name__ == "__main__":
    unittest.main()

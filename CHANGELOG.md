# Changelog

## 0.4.1 (2026-10-20)

`pr-toolbox motion` is faster, with the same findings.

| On OpenEMR's 302 open PRs, 4 threads | 0.4.0 | 0.4.1 |
|---|---|---|
| Full build | 1,183 s | 397 s |
| Rebuild after the base moved 12 merges | 978 s | 249 s |
| Rebuild when nothing moved (audit stage) | 269 s | 11 s |

- **Master's definitions, indexed once:** class and function definitions are indexed in one pass, and callers of removed names are found with three batched searches, instead of one whole-tree search per name.
- **Subclasses in one search:** subclasses of changed classes are found in one search and read through one `git cat-file` process.
- **Stacked results cached:** cached by the two PRs' heads, since they don't depend on the base.
- **Parallel merges:** merges onto the base, re-merges, caps checks and pair merges run on `--threads` threads.
- **Timings:** the audit summary records each stage's time, and `motion compare` ignores them.

Each figure was checked by comparing findings with the previous version's for the same commits. They are identical in every category.

## 0.4.0 (2026-10-20)

New command: **`pr-toolbox motion`**, a map of every open PR for the people who merge.

- Every open PR merged onto the base branch, and every pair sharing a file merged on top of that; conflicts split into code and mechanical files.
- Breaks that merge cleanly: deleted files still changed, stacked PRs, the same method changed twice, version-number and exact-cap collisions, shared names; for PHP, removed functions still called and signature changes an override would no longer satisfy.
- The plan per area, quoted from the project's own documents and checked word for word on every build; a path forward; a triage table with a "Nothing blocking" view; evidence and reproduction commands on every PR card.
- Controls: known cases each check must find before the page is written.
- Incremental rebuilds when the base moves, with a self-check; `--master` to replay; `motion compare` to prove two builds agree.
- Optional reading for semantic clashes: `motion candidates`, `motion packets`, reader instructions.
- The OpenEMR profile gains a `motion` section.

Nothing changed in `landing`, `health` or `check`.

## 0.3.0 (2026-10-19)

Renamed **pr-toolbox**, since the tools work on any GitHub repository.

- **Repository:** https://github.com/anun333/pr-toolbox. GitHub redirects the old address.
- **Command:** `pr-toolbox`. `pr-tools` and `openemr-pr-tools` still work.
- **Python package:** `pr_toolbox`. `openemr_pr_tools` still imports, and gives the same modules.
- **Profile file:** `.pr-toolbox.json`. `.pr-tools.json` is still read.
- **Environment:** `PR_TOOLBOX_REPO`, `PR_TOOLBOX_REMOTE`, `PR_TOOLBOX_GITHUB_REPO` and `PR_TOOLBOX_BASE_BRANCH`. The `PR_TOOLS_*` and `OPENEMR_*` names are still read.
- **Default state directory:** `.pr-toolbox/`. Pass `--state-dir .pr-tools` to keep using 0.2's.

Nothing else changed.

## 0.2.0 (2026-10-19)

The tools now work on any GitHub repository.

- **Target.** The repository comes from the checkout you're in, or `--repo`. The GitHub repository is read from the `upstream` (else `origin`) remote's URL, or given with `--github-repo`. The base branch is the remote's default branch, or `--base-branch`. The same choices can be set in `PR_TOOLS_REPO`, `PR_TOOLS_REMOTE`, `PR_TOOLS_GITHUB_REPO` and `PR_TOOLS_BASE_BRANCH`.
- **Profiles.** A project's rules and CI commands live in a profile: `.pr-tools.json` in the checkout, `--config FILE`, or a built-in one with `--preset`. OpenEMR's rules moved into the built-in `openemr` profile, which is chosen automatically for `openemr/openemr`. These are now profile keys rather than code:
  - `version_claims` (`v_database`);
  - `exact_caps` (the PHPStan caps);
  - `check_steps` (php-lint, phpcs, rector, PHPStan, tests).
- **`check`** runs the profile's `check_steps`, on the host or in a container, once with all matching files or once per file. `--list` shows the steps.
- **Command.** It's also installed as `pr-tools`; `openemr-pr-tools` still works.

Changes for users of 0.1.0:
- **Default checkout.** The default is now the current directory's repository, not `~/dev/openemr`. `OPENEMR_REPO` and `OPENEMR_GITHUB_REPO` are still read.
- **Default state directory.** It's now `.pr-tools/`, not `.openemr-pr-tools/`. Pass `--state-dir` to keep the old one.
- **`check` step names.** `upgrade-sql` is now `version` and `v-database` is now `version-claims`. `tests` is split into `tests-isolated` and `tests`. A new `merges` step runs first.
- **The landing line's stamp** no longer mentions the PHPStan baseline. It names the base branch.

## 0.1.0 (2026-10-19)

First release, three commands:

- `landing`: which open PRs each of your OpenEMR PRs conflicts with, from test merges against master and every open PR that shares a file (the PHPStan baseline included). It also lists the `v_database` claims among open PRs and exact baseline-cap collisions, and can write a "Landing order" line at the top of your own PR descriptions when the facts change.
- `health`: what changed on your open PRs since the last look. It covers CI, conflicts with master, review threads waiting on you, review decisions, merges and closures, and a draft's dependencies merging.
- `check`: what OpenEMR's CI will say about a branch before you push. It runs syntax, phpcs, rector, codespell, full PHPStan with CI's configuration, the changed tests, exact caps, the upgrade-script and `v_database` rules, a secrets scan and commit trailers.

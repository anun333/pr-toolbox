# Changelog

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

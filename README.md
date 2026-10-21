# pr-toolbox

Quiet, local tools for people who send pull requests to a busy GitHub project:

| Command | What it answers |
|---|---|
| `pr-toolbox landing` | Which open PRs does each of my PRs conflict with, and in what order should they land? |
| `pr-toolbox health` | What changed on my open PRs since I last looked: CI, conflicts with the base branch, review threads, merges? |
| `pr-toolbox check` | What will the project's CI say about this branch, before I push it? |
| `pr-toolbox motion` | For maintainers: what does merging each open PR do to the others, and where is each area of the code going? |

Large projects can have hundreds of open pull requests and a few volunteers to review them. These tools help a contributor:
- arrive with a branch that's green the first time;
- say up front what their PR conflicts with;
- notice what changed on their PRs.

`motion` is for the people who merge: one page covering every open PR at once.

They work on any GitHub repository. They were first written for [OpenEMR](https://github.com/openemr/openemr), which has a built-in profile (see [The OpenEMR profile](#the-openemr-profile)). The command is `pr-toolbox`. `pr-tools` and `openemr-pr-tools`, its names before 0.3.0, also work.

They don't comment, label, ping or re-run anything on GitHub. `health` and `check` only read. `landing` reads too, unless you pass `--apply` or `--rewrite`. Then it rewrites the first paragraph of your own PRs' descriptions, and only when the facts behind that paragraph changed. Editing a description sends nobody a notification.

## Install

```
pipx install git+https://github.com/anun333/pr-toolbox@v0.3.0
```

Or clone the repository and run `./pr-toolbox` from it; nothing needs installing.

**Requirements**
- Python 3.10 or later.
- git 2.38 or later, for in-memory test merges with `merge-tree --write-tree`.
- The [GitHub CLI](https://cli.github.com/), logged in (`gh auth login`).
- A clone of the project.
- For `check` steps that run in a container: Docker, and the container running.

## Which repository

Run the commands inside a clone of the project, or point at one:

- **The checkout:** `--repo DIR`, else `$PR_TOOLBOX_REPO`, else the repository you're in.
- **The remote:** `--remote NAME`, else `$PR_TOOLBOX_REMOTE`, else `upstream` if the clone has one, else `origin`. Branches and PR heads are fetched from it.
- **The GitHub repository:** `--github-repo owner/name`, else `$PR_TOOLBOX_GITHUB_REPO`, else read from the remote's URL.
- **The base branch:** `--base-branch NAME`, else `$PR_TOOLBOX_BASE_BRANCH`, else the remote's default branch.

So in a fork cloned with an `upstream` remote, `pr-toolbox landing` compares your PRs with the upstream project's open PRs and default branch, without any options.

The older names still work as fallbacks: `PR_TOOLS_*` from 0.2, and `OPENEMR_REPO` and `OPENEMR_GITHUB_REPO` from 0.1.

## Project profiles

A profile tells the tools a project's own rules and CI commands. They look for one in this order:
1. `--config FILE`;
2. `--preset NAME`, a built-in profile;
3. `.pr-toolbox.json` at the top of the checkout (or `.pr-tools.json`, its name in 0.2);
4. the built-in profile for the GitHub repository, if there is one (only `openemr/openemr` so far).

Without a profile you still get:
- test merges;
- PR health;
- the built-in checks: merging onto the base branch, codespell, secrets and trailers.

A profile is a JSON object; every key is optional:

| Key | Used by | Meaning |
|---|---|---|
| `name`, `description` | `check --list` | what the profile is for |
| `check_steps` | `check` | the project's own commands; see below |
| `container` | `check` | `{"app_dir": "/path"}`: where the checkout is mounted in the container |
| `codespell_exclude` | `check`, `landing` | globs codespell skips |
| `shared_ignore` | `landing` | globs of files many PRs touch in passing, never named as "shared" (default: `codespell_exclude`) |
| `health_ignore` | `health` | regex of CI check names to leave out (default `^codecov/`) |
| `version_claims` | `landing`, `check` | a version number that every schema change takes; see below |
| `exact_caps` | `landing`, `check` | counts that one file pins exactly; see below |

This repository's own [`.pr-toolbox.json`](.pr-toolbox.json) is a small example.

### `check_steps`

Each step runs one command, in order, after the built-in `merges` step and before the other built-in steps:

```json
{"name": "lint", "where": "host", "files": "*.py", "run": "ruff check {files}"}
```

| Field | Meaning |
|---|---|
| `name` | the step's name in the output and in `--skip` (required) |
| `run` | the shell command (required) |
| `where` | `host` (the default) runs it in the checkout. `container` runs it with `docker exec` in the container that mounts the checkout at `container.app_dir`. |
| `files` | a glob or list of globs. The command runs once, with the matching changed files in `{files}`. With neither `files` nor `each`, every changed file matches. |
| `each` | like `files`, but the command runs once per matching file, with the file in `{file}` |
| `exclude` | globs to leave out of the match |
| `always` | run even when no changed file matches, for whole-project analysis |
| `write` | `{"path": …, "content": …}`: a file written (in the container, for container steps) before the command runs |
| `fail_pattern` | a regex. Output matching it fails the step even when the command exits 0. |
| `timeout` | seconds (default 1800) |
| `tail` | lines of output shown when the step fails (default 15) |
| `verbose` | for `each`: show each file's last line of output, not just `ok` |

Commands and `write` content can use these placeholders:
- `{files}` and `{file}`, shell-quoted;
- `{app_dir}`;
- `{workers}`, from `--workers`, for tools that would otherwise start a process per core;
- `{base}`, the base branch as `remote/branch`.

A step whose command exits non-zero fails. A step with no matching files is skipped.

### `version_claims`

Some projects make every schema change take the next number in one file, so two PRs that take the same number conflict. The rule says where that number lives:

| Field | Meaning |
|---|---|
| `file` | the file that holds the number |
| `pattern` | a regex whose first group is the number |
| `related` | globs of the files a renumbering touches. A conflict confined to these is reported as a numbering conflict, not a textual one. |
| `bump_when` | globs of changes that need a new number |
| `mirrors` | `[{"file", "pattern"}]`: other places the same number appears, which must agree |
| `blocks` | `{"files", "open", "close"}`: files whose opening and closing markers must balance, such as conditional blocks in upgrade scripts |
| `noun` | what to call the number in sentences, such as `` `v_database` `` |

### `exact_caps`

Some projects pin counts exactly, for example how many baseline entries a static-analysis rule may have. Two PRs that each lower the same count pass CI alone and fail together once both land. The rule says how to read those counts:

| Field | Meaning |
|---|---|
| `file` | the file that pins them |
| `block` | a regex whose first group is the list of pinned counts |
| `entry` | a regex with two groups, a name and its count, matched within the block |
| `counted` | the path of the file each count describes, with `{name}` |
| `marker` | the string counted in that file |
| `noun` | what to call a count in sentences |

## `landing`: landing order

```
pr-toolbox landing [--author LOGIN] [--state-dir DIR] [--if-due] [--apply]
pr-toolbox landing --show 14708           # the line the last check gives PR 14708
pr-toolbox landing --rewrite 14708 14711  # put those lines at the top of those PRs' descriptions
```

For each of your open PRs it merges the PR onto the base branch in memory. It then test-merges the result with every open PR that changes any of the same files. It reports three kinds of trouble:

- **Textual conflicts**, with the files.
- **Version-number claims**, when the profile has `version_claims`: which open PRs take the same number.
- **Exact-cap collisions**, when the profile has `exact_caps`. The tool checks each merged pair against each PR alone to find pairs that pass separately and fail together.

PRs that already conflict with the base branch are left out of each comparison. They have to rebase before they can land, and what they'll conflict with after that can't be known yet.

It writes `landing-order.md` and `landing-order.json` in the state directory (`.pr-toolbox/` by default) and prints a JSON summary. With `--if-due` it does nothing unless the base branch or one of your PR heads moved since the last check. A full check of 20 PRs against 300 open ones takes a minute or two in one process.

**The line** it writes reads like this:

> **Landing order:** it conflicts with #14510 in `interface/patient_file/summary/immunizations.php`. Either can land first, and if #14510 does, I'll rebase this one. (test-merged on 2026-10-19 against master 7846cf5c12 and every open PR that changes any of the same files.)

**Hand-written context** goes in `landing-order-notes.json` in the state directory, keyed by PR number:

```json
{
  "14708": {"prefix": "**Landing order** ([#14710](https://…)): it doesn't depend on #14711. #14715 builds on it."},
  "14715": {"prefix": "**Landing order:** after #14708 and #14711, and a draft until they land.",
            "carries": [14708, 14711],
            "carries_text": "It carries copies of their commits, so it conflicts with both until then.",
            "depends_on": [14708, 14711]}
}
```

- `prefix` replaces the opening words, and `suffix` is added before the stamp.
- `carries` names PRs whose commits a stacked draft contains. The draft and those PRs then aren't reported as conflicting with each other.
- `sql_text` replaces the generated sentence about the version-number group.
- `depends_on` is read by `health`.

## `health`: what changed on your PRs

```
pr-toolbox health [--author LOGIN] [--state-dir DIR] [--ignore REGEX]
```

It keeps a snapshot of your open PRs and prints, as JSON, what changed since the last one:
- CI starting to fail, with the failing checks named, or passing again. Checks matching the profile's `health_ignore` (or `--ignore`) are left out.
- GitHub reporting a conflict with the base branch.
- Review threads whose last comment isn't yours, counting people and bots separately.
- An approval or a request for changes.
- A PR merged, and by whom, or closed.
- A dependency merging, from `depends_on` in the notes file. The event says "rebase it", and "mark it ready" when it's a draft and nothing else is still open.

GitHub often reports mergeability as unknown until it has computed it, so an unknown answer keeps the last known one. `landing` tests conflicts with the base branch itself.

## `check`: before you push

```
pr-toolbox check [--list] [--container auto|NAME|none] [--app-dir DIR] [--workers 3] \
    [--secrets-env FILE --public-keys KEY,KEY] \
    [--forbid-trailer REGEX] [--require-trailer REGEX] [--skip STEP,…] [--json]
```

It compares the checkout with the base branch, counting committed, uncommitted and untracked changes. Then it runs the steps in this order: `merges`, the profile's `check_steps`, and the remaining built-in steps. `--list` prints the steps without running them.

| Built-in step | What it checks |
|---|---|
| `merges` | the branch merges onto the base branch without conflicts |
| `codespell` | codespell on the changed files, when it's installed |
| `caps` | the profile's exact caps, on the branch merged onto the base (committed work) |
| `version` | when versioned files changed: the number is above the base's, its mirrors agree, and blocks balance |
| `version-claims` | when the version file changed: whether another open PR claims the same number |
| `secrets` | whether any value from `--secrets-env` (a `KEY=VALUE` file) appears in the diff. It names keys, never values. |
| `trailers` | commit messages against `--forbid-trailer` and `--require-trailer` patterns |

A step that can't run counts as a failure, not a pass. The exit code is 1 when any step fails.

**Container.** `--container auto` finds the running container that mounts the checkout at `container.app_dir`.

**CPU.** The steps run one after another. `--workers` fills `{workers}` in the profile's commands.

## `motion`: a map for maintainers

```
pr-toolbox motion                 # fetch, audit, controls, page: ~20 minutes for 300 PRs, less when little moved
pr-toolbox motion --skip-fetch    # rebuild from the inputs already in the cache
```

It writes one self-contained HTML page (`~/.cache/pr-toolbox-motion/work-in-motion.html` unless `--out` says otherwise):
- **Every open PR merged onto the base branch,** and every pair of PRs that shares a file merged on top of that, with `git merge-tree` in memory. Conflicts are split into code and mechanical files (version files, migrations, lint baselines), so a rebase with no code decision in it says so.
- **Breaks that merge cleanly as text:** a file one PR deletes and another changes; one PR built on another; the same method changed twice; for PHP, a removed function still called and a signature change an override elsewhere would no longer satisfy; two PRs claiming one version number (`version_claims`); two PRs that each keep an exact lint cap and break it together (`exact_caps`); names two PRs both add (routes, tables, settings: whatever the profile lists).
- **The maintainers' plan per area,** quoted from their own documents and issues, each quote checked word for word against its source on every build: a quote that drifts stops the build.
- **A path forward:** decide between overlapping PRs, review what nothing blocks, rebase where the fix is mechanical, then pick up new work in each plan's own order, on files no open PR changes.
- **A triage table** of every open PR with CI, maintainer review, conflicts, breaks, fit with the plan and size, filterable. Its "Nothing blocking" view is not a review: it says only that reviewing a PR won't be wasted on a rebase or a collision.
- **Evidence on every claim:** each PR card names the commits it was computed on and the git commands that reproduce it.

Caches are keyed by the base's commit and each PR's head and merge base. When the base moves, only PRs and pairs touching a file it changed are merged again; reused results are re-merged and compared whenever they are needed, and a disagreement is reported (`incremental.rule_violations`). `--master <sha>` replays a build at a pinned base, and `pr-toolbox motion compare A B` checks that two findings files for the same commits agree.

**Controls.** Every build first checks known cases from the profile: each must be found (or, for a negative case, must not be), or the build stops instead of printing a silent zero. The page lists the results.

**Reading for semantic clashes** (optional, judgement). Two PRs can merge cleanly and still disagree about one concept in different files. `pr-toolbox motion candidates` picks pairs from different authors that meet on one concept (a domain resource, a table both write, a route family, a method one changes and the other newly calls), weighted by how few PRs share it. `pr-toolbox motion packets` writes one packet per pair, with each PR's mention of its partner withheld so a reading rests on the code. Readers, people or agents following [`semantic_reader_prompt.md`](pr_toolbox/motion/semantic_reader_prompt.md), write a verdict per pair, a second reading tries to refute each clash, and the page shows what survives, labelled as judgement.

**Profile keys.** A `motion` section in the profile sets the project's name, mechanical files, release-tag pattern, labels and paths to leave out, bots, the files that count as code, the symbol reader (`"php"`, or none), shared-name kinds, upgrade-file rules, semantic anchors, and `streams` (the areas: quoted targets with their sources, plan issues, retired patterns, how each plan cuts its remaining work) and `controls`. Every key has a neutral default; the [OpenEMR profile](pr_toolbox/presets/openemr.json) sets them all.

**Requirements.** As for the other commands, plus `docker` or `php` for the PHP symbol checks (the pinned `php:8.3-cli` image is preferred so every build reads code with the same tokenizer); without either, those checks are skipped and the page says so.

**Care.** The page names every open PR and its author, with failing CI and conflicts. It is generated locally and posted nowhere; show it to the project's maintainers before anyone else.

**Prior art.** Merge queues (GitHub's, Mergify, bors) test one combination at merge time and only for what tests cover; Gerrit lists conflicts with other changes; ConflictBot and Rust's triagebot comment on PRs. `motion` looks ahead across every open PR at once, adds the project's plan, and comments nowhere.

## The OpenEMR profile

The built-in [`openemr`](pr_toolbox/presets/openemr.json) profile is what the tools were first written for, and it's a worked example of every key. For a clone of `openemr/openemr` it's used automatically.

- **`version_claims`**: `$v_database` in `version.php`. Every SQL change takes the next number, mirrored in the header of `sql/database.sql`. `#If…`/`#EndIf` blocks in the upgrade scripts must balance.
- **`exact_caps`**: `.phpstan/fatal-baseline-caps.php`, which pins some PHPStan baseline counts exactly.
- **`check_steps`**: the checks OpenEMR's CI runs, in the container of a dev stack such as [`openemr-cmd`](https://github.com/openemr/openemr-devops/tree/master/utilities/openemr-cmd) starts:
  - `php-lint`: `php -l` on each changed PHP file;
  - `phpcs`, with `phpcs.xml.dist`;
  - `rector`: a dry run;
  - `phpstan`: the full analysis with CI's `.phpstan/phpstan.ci.neon`, limited to `--workers` processes;
  - `tests-isolated` and `tests`: each changed test file, with `phpunit-isolated.xml` or `phpunit.xml`.

- **`motion`**: twelve areas with targets quoted from `AGENTS.md` and the maintainers' issues, the patterns each plan retires, how each plan cuts its remaining work, OpenEMR's shared-name kinds (routes, tables, upgrade columns, list ids, global settings, menu ids, events), FHIR resources as semantic anchors, the PHP symbol reader, and controls drawn from real open PRs. Access-control code is left out.

OpenEMR asks that AI-assisted commits say so, for example:

```
pr-toolbox check --require-trailer '^Assisted-by: ' --forbid-trailer '^Co-Authored-By: Claude'
```

## Running them on a schedule

`health` and `landing --if-due` are cheap when nothing moved, and print JSON, so they fit an hourly cron job or a watcher script. A typical setup:
- run `health` every hour;
- run `landing --if-due` every hour;
- run `landing --apply` by hand when its report shows a change you want on your PRs.

The `pr_toolbox` modules can also be imported. `common.resolve()` returns the target, `landing_order.check(target, author)` returns the landing facts, and `pr_health.snapshot(github, author, ignore)` returns the health snapshot.

## Limits

- **Textual conflicts and the profile's rules only.** Two PRs can merge cleanly and still not work together; `motion`'s reading step is the only check for that, and it is judgement.
- **`motion` compares pairs, not larger combinations,** and lists PRs over its file limit instead of comparing them pair by pair. Its signature check follows one level of inheritance.
- **A snapshot.** Any merge or push can change the answer, which is what `--if-due` is for.
- **`check` is only as good as its profile.** Its steps are the commands you give it, and a step that's missing isn't run.

## Licenses

The code is under the GNU General Public License, version 3 or later (`LICENSE`). This README, the CHANGELOG and other documentation are dedicated to the public domain under CC0 1.0 (`LICENSES/CC0-1.0.txt`).

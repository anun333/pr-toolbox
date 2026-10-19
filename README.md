# openemr-pr-tools

Quiet, local tools for people who send pull requests to [OpenEMR](https://github.com/openemr/openemr):

| Command | What it answers |
|---|---|
| `openemr-pr-tools landing` | Which open PRs does each of my PRs conflict with, and in what order should they land? |
| `openemr-pr-tools health` | What changed on my open PRs since I last looked: CI, conflicts with master, review threads, merges? |
| `openemr-pr-tools check` | What will OpenEMR's CI say about this branch, before I push it? |

OpenEMR has hundreds of open pull requests, and most of the people who review them are volunteers. These tools help a contributor arrive with a branch that's green the first time, say up front what it conflicts with, and notice what changed on their PRs.

They don't comment, label, ping or re-run anything on GitHub. `health` and `check` only read. `landing` reads too, unless you pass `--apply` or `--rewrite`. Then it rewrites the first paragraph of your own PRs' descriptions, and only when the facts behind that paragraph changed. Editing a description sends nobody a notification.

## Install

```
pipx install git+https://github.com/anun333/openemr-pr-tools@v0.1.0
```

Or clone the repository and run `./openemr-pr-tools` from it; nothing needs installing.

**Requirements**
- Python 3.10 or later.
- git 2.38 or later, for in-memory test merges with `merge-tree --write-tree`.
- The [GitHub CLI](https://cli.github.com/), logged in (`gh auth login`).
- A clone of OpenEMR with a remote that points at `openemr/openemr`. By default the remote is `upstream` and the clone is at `~/dev/openemr`; set `OPENEMR_REPO` or pass `--repo` otherwise.
- For `check` only: Docker and a running dev stack for the worktree, as [`openemr-cmd`](https://github.com/openemr/openemr-devops/tree/master/utilities/openemr-cmd) starts one.

## `landing`: landing order

```
openemr-pr-tools landing [--author LOGIN] [--state-dir DIR] [--if-due] [--apply]
openemr-pr-tools landing --show 14708           # the line the last check gives PR 14708
openemr-pr-tools landing --rewrite 14708 14711  # put those lines at the top of those PRs' descriptions
```

For each of your open PRs it merges the PR onto master in memory. It then test-merges the result with every open PR that changes any of the same files, the PHPStan baseline included. Three kinds of trouble are reported:

- **Textual conflicts**, with the files.
- **`v_database` claims.** Every PR that changes the schema takes the next number in `version.php`, and two PRs that take the same number conflict.
- **Exact-cap collisions.** OpenEMR's `.phpstan/fatal-baseline-caps.php` pins some baseline counts exactly. Two PRs that each lower the same count pass CI alone and fail together once both land. The tool checks each merged pair against each PR alone to find these.

PRs that already conflict with master are left out of each comparison. They have to rebase before they can land, and what they'll conflict with after that can't be known yet.

It writes `landing-order.md` and `landing-order.json` in the state directory (`.openemr-pr-tools/` by default), and prints a JSON summary. With `--if-due` it does nothing unless master or one of your PR heads moved since the last check. Running from a scheduler, a full check of 20 PRs against 300 open ones takes a minute or two in one process.

**The line** it writes reads like this:

> **Landing order:** it conflicts with #14510 in `interface/patient_file/summary/immunizations.php`. Either can land first, and if #14510 does, I'll rebase this one. (test-merged on 2026-10-19 against master 7846cf5c12 and every open PR that changes any of the same files, the PHPStan baseline included.)

**Hand-written context** goes in `landing-order-notes.json`, keyed by PR number:

```json
{
  "14708": {"prefix": "**Landing order** ([#14710](https://…)): it doesn't depend on #14711. #14715 builds on it."},
  "14715": {"prefix": "**Landing order:** after #14708 and #14711, and a draft until they land.",
            "carries": [14708, 14711],
            "carries_text": "It carries copies of their commits, so it conflicts with both until then.",
            "depends_on": [14708, 14711]}
}
```

- `carries` names PRs whose commits a stacked draft contains. The draft and those PRs then aren't reported as conflicting with each other.
- `sql_text` replaces the generated sentence about the `v_database` group.
- `depends_on` is read by `health`.

## `health`: what changed on your PRs

```
openemr-pr-tools health [--author LOGIN] [--state-dir DIR] [--ignore REGEX]
```

It keeps a snapshot of your open PRs and prints, as JSON, what changed since the last one:
- CI starting to fail, with the failing checks named, or passing again. Codecov and the "All Checks Passed" summary are left out by default.
- GitHub reporting a conflict with master.
- Review threads whose last comment isn't yours, from people and from bots counted separately.
- An approval or a request for changes.
- A PR merged, and by whom, or closed.
- A dependency merging, from `depends_on` in the notes file. The event says "rebase it", and "mark it ready" when it's a draft and nothing else is still open.

GitHub often reports mergeability as unknown until it has computed it, so an unknown answer keeps the last known one. `landing` tests conflicts with master itself.

## `check`: before you push

```
openemr-pr-tools check [--worktree DIR] [--container auto] [--workers 3] \
    [--secrets-env FILE --public-keys KEY,KEY] \
    [--forbid-trailer REGEX] [--require-trailer REGEX] [--skip STEP,…] [--json]
```

Run it in an OpenEMR worktree whose dev stack is up. It compares the branch with `upstream/master`, counting committed and uncommitted changes:

| Step | What it runs |
|---|---|
| `php-lint` | `php -l` on each changed PHP file, in the container |
| `phpcs` | `vendor/bin/phpcs --standard=phpcs.xml.dist` on the changed PHP files |
| `rector` | `vendor/bin/rector process --dry-run` on them |
| `codespell` | codespell on the changed files, when it's installed |
| `phpstan` | the full analysis with CI's `.phpstan/phpstan.ci.neon`, limited to `--workers` processes |
| `tests` | each changed `…Test.php`, with `phpunit-isolated.xml` or `phpunit.xml` |
| `caps` | the exact baseline caps on the branch merged onto master (committed work) |
| `upgrade-sql` | when SQL changed: balanced `#If`/`#EndIf` blocks, a `v_database` above master's, and `version.php` agreeing with `database.sql` |
| `v-database` | when `version.php` changed: whether another open PR claims the same number |
| `secrets` | whether any value from `--secrets-env` (a `KEY=VALUE` file) appears in the diff; it names keys, never values |
| `trailers` | commit messages against `--forbid-trailer` and `--require-trailer` patterns |

A step that can't run counts as a failure, not a pass. The exit code is 1 when any step fails.

**Container.** `--container auto` finds the running container that mounts the worktree at `--app-dir` (default `/var/www/localhost/htdocs/openemr`), as openemr-cmd's stacks do.

**Trailers.** OpenEMR asks that AI-assisted commits say so, for example:

```
openemr-pr-tools check --require-trailer '^Assisted-by: ' --forbid-trailer '^Co-Authored-By: Claude'
```

**CPU.** Full PHPStan otherwise starts a worker per core; `--workers` caps it. The steps run one after another.

## Running them on a schedule

`health` and `landing --if-due` are cheap when nothing moved and print JSON, so they fit an hourly cron job or a watcher script. A typical setup:
- run `health` every hour;
- run `landing --if-due` every hour;
- run `landing --apply` by hand when its report shows a change you want on your PRs.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `OPENEMR_REPO` | `~/dev/openemr` | the OpenEMR clone `landing` fetches into |
| `OPENEMR_GITHUB_REPO` | `openemr/openemr` | the repository whose pull requests are read |

## Limits

- **Textual conflicts and exact caps only.** Two PRs can merge cleanly and still not work together; reading them is the only check for that.
- **A snapshot.** Any merge or push can change the answer, which is what `--if-due` is for.
- **`check` assumes an openemr-cmd stack.** Its tool paths are those of OpenEMR's own `composer.json` and container images.

## Licenses

The code is under the GNU General Public License, version 3 or later (`LICENSE`), like OpenEMR. This README, the CHANGELOG and other documentation are dedicated to the public domain under CC0 1.0 (`LICENSES/CC0-1.0.txt`).

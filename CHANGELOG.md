# Changelog

## Unreleased

- **`motion check-page`: a stack's version bump with an outside claimant.** The check that a stacked pair's shared bump isn't called a collision now leaves out stacks whose number another open PR also claims, and checks that those are called a collision naming that PR. On OpenEMR, #14808 took the same `v_database` number as the stacked #14708 and #14715, and the page rightly said SHARED, which the old check failed.
- **`check`: the OpenEMR profile checks JavaScript and CSS too.** It runs `eslint` (errors only, as CI's `npm run lint:js` does), `stylelint`, and each changed Jest test in `tests/js`, in the checkout. Before, a branch that changed only JavaScript passed every project step. Built-in `codespell` already read those files.
- **`check_steps`: `needs`.** A step can name the files it needs, such as `node_modules/.bin/eslint`. When one is missing, the step is skipped and says which, with `needs_hint` saying how to get it, instead of failing as if the code were wrong.
- **A Decidim profile** (`decidim/decidim`, base `develop`), picked automatically for that repository. It reads CI as run when a `Lint code` check is there, which runs on every PR, while each module's tests run only when its files change. Its mechanical files are the lockfiles, `RELEASE_NOTES.md`, the RuboCop to-do list, `i18n-tasks.yml` and the locale files Crowdin manages. Its shared names are migration classes, new tables, content blocks, `Decidim::Env` variables and `Decidim` settings. It has three controls, each reproduced by hand with `git merge-tree`. It has no areas yet, and the page says so instead of showing an empty section.
- **`motion`: PRs aimed at another branch are named, not compared.** A release branch's backport merges onto that branch; merged onto the base it carried every commit between the two. On Decidim that made two backports of 2,000+ files each and nine false shared names. They are now listed under the page's limits, and the "since the last build" panel shows a PR retargeted to another branch as such, not as closed. On Decidim the findings are otherwise the same as before; every open OpenEMR PR targets `master`, so its page is unchanged.
- **`motion`: "closed without merging" needs GitHub to say closed.** The "since the last build" panel asked only whether a PR that left the list was merged, so a PR still open read as closed.
- **`motion`: GitHub's limits, met rather than trusted.**
  - **Timeouts:** a page of PRs with many checks each can run past GitHub's 10-second limit (25 Decidim PRs took 9.8s). A page that fails is asked for again at half the size.
  - **Short searches:** under load, search can end a window early with no error, on a page whose count reads 0, the first page included. The maintainers it found changed from one build to the next. A window now counts only when every page gives the same count and that many merges came back. Otherwise it is read again, then halved, and a single day that stays short stops the build. Decidim's year of merges now matches the REST search's total exactly (1,914).

## 0.7.0 (2026-10-22)

- **`motion`: gaps in conformance, by the kind of evidence.** Each standard in the conformance lens now says:
  - **measured on this build:** whether its official test runs in CI (a `ci_re` searched in the base branch's workflow files), and for a `names` list (US Core's profiles, say) how many are named in the code;
  - **reported:** the open issues a label or query finds, fetched fresh each build, with a link to the search;
  - **known:** gaps the profile cites, each with its state read from GitHub (open, fix open, merged or closed) or a short kind for a source outside GitHub.

  The section's summary line counts the certified standards with no official test in CI, and a project-wide label (`conformance.issues`) lists open issues on certification itself.
- **Controls for the measured checks:** a CI control must find exactly the expected workflow files and a made-up tool must find none; a names control searches the code for a planted name that must come out missing, alongside the real ones that must be found. A broken search stops the build instead of reporting every name missing.
- **The page check opens every standard at phone width** and measures the page's width as well as each element's. A pill's text can spill past its own box, which no element's box shows; a planted long pill now fails the check.
- **The OpenEMR profile** adds the HIPAA X12 transaction standards (837P, 837I, 835) and the gaps known today: the CapabilityStatement's sort and paging claims (raised at the 2026-10-20 meeting), C-CDA import gaps, eCQM and QDM records, the discontinued interaction API, public-health HL7 export, the 837I facility code and NPI and ZIP handling, and an axe-core scan of the patient portal (8 WCAG 2.1 A and AA rules failing, 2 of them critical).

## 0.6.1 (2026-10-22)

- **The OpenEMR conformance profile now comes from CHPL:** OpenEMR 8's listing (11757), read through the CHPL API. It has the same 32 criteria as OpenEMR's wiki; standards with their SVAP updates ((g)(10): US Core 6.1.0 updated to 8.0.0, SMART 2.0.0 to 2.2.0, Bulk Data 1.0.0 to 2.0.0, USCDI v3 to v5; (b)(1): C-CDA R2.1 with Companion Guide 4.1); and how each criterion was certified (test procedure with a tool, gap certification via the 2025 attestation, or attestation alone). The page shows each criterion's method and CHPL's official findings for the listing: no surveillance, no non-conformities and no direct reviews, as of 2026-10-22.

## 0.6.0 (2026-10-22)

- **`motion`: a conformance lens.** A profile's `motion.conformance` lists the outside standards the project answers to (certification criteria, rules), each with the code paths that carry it, the official tool that tests it and whether that tool can run on a PR before merge, what a pending rule would do to it, and short cited guidance. The page gains a Conformance section with one folded entry per standard and the open PRs touching it, a Standards panel on each PR card, a "Touches a certified criterion" triage filter, and standards in path lookups. It says what a PR touches, not whether it conforms.
  - **Controls stop a bad build:** every path prefix must match a file on the base branch, each control's files and title must match exactly the standards it expects (a docs-only change none), and a control naming an open PR must match that PR. A control naming a PR that has closed is reported, not failed.
  - **The OpenEMR profile** lists the criteria OpenEMR 8 is certified for, from OpenEMR's own wiki (not yet checked against CHPL, which refused the requests), with the HTI-5 proposal's action for each (proposed, not final, as of 2026-10-22), plus three it isn't certified for but implements: public-health HL7 v2, e-prescribing, and Section 504 accessibility for patient-facing pages. Every mapping is marked provisional, and the privacy and security criteria are labelled for the maintainers' review, never analysed.
- **`motion`: "CI not run" when the real CI never started.** GitHub holds the Actions workflows of a first-time contributor's fork PR until a maintainer approves them; until then the head carries only app checks (DCO, CodeRabbit, auto-merge), and "nothing failing" read as passing. A profile's `motion.ci_required` names a check that shows CI ran; without a match the PR reads "CI not run" and stays out of "Nothing blocking". A failing check still reads as failing, and a profile without the key reads CI as before. The OpenEMR profile sets `^(PR Title|phpstan|Rector PHP Analysis)\b`, present on all 216 open PRs whose CI ran and none of the 85 whose CI didn't (2026-10-21). In that morning's build, 27 of 77 "Nothing blocking" PRs had never run CI.

## 0.5.0 (2026-10-21)

- **`pr-toolbox motion check-page`:** opens a built page in a headless browser, at desktop and phone widths and in dark mode, and checks what a reader meets. That covers:
  - script errors, sideways scrolling, empty sections, links to nowhere and code printed as text;
  - whether the counts, filters, groups, sorting, folded sections, lookup and cards do what they say;
  - that the page and the build agree on "Nothing blocking".

  It runs with this Python's Playwright if installed, otherwise in Playwright's Docker image, and exits 1 if any check fails. Each check was first shown to fail on a page that had the problem.
- **Group diagrams:** every group is drawn: circles for its PRs, lines styled by why two are linked, circles marking "Nothing blocking", failing CI and drafts. Selecting a circle, or pressing Enter on it, opens that PR's card.
- **Triage marks only what is out of the ordinary:** CI passing, merging cleanly, no review yet and a free version number are plain text; pills are kept for what needs attention.
- **Hover titles:** every PR number shows its title and author on hover.
- **Phones:** the summary strip comes before the section links, and the tiles and header drop their small print, so the counts are on the first screen.
- **The skill** covers checking the page, what each count on the page means, the "since" history, and eight new traps.
- **Tests:** the skill's test reads the real subcommands from the command line's own source.

## 0.4.9 (2026-10-21)

- **Since the last build:** a panel under the summary strip lists what changed. It compares with the newest build at least 12 hours older (`WIM_SINCE_HOURS`), so rebuilding twice in a day still reads as "since yesterday"; until a day of history exists, it compares with the oldest build kept. It covers:
  - PRs merged or closed (asking GitHub which, unless `WIM_OFFLINE` is set);
  - new PRs;
  - PRs that entered or left "Nothing blocking", with why;
  - new code conflicts;
  - PRs that newly need a rebase;
  - maintainer approvals and change requests.

  Each build saves a small snapshot in the cache's `history/` (the newest 30 are kept).
- **Shorter page:** about 39 desktop screens down to 19 on OpenEMR.
  - **Areas:** each area folds to one line with its open PRs, plan issues and latest count with its trend.
  - **Path forward:** each area's remaining work folds the same way.
  - **Triage:** shows 25 rows, then "Show all".
  - **Links:** a link into a folded section opens it.
- **"Nothing blocking" is also decided in Python,** for the "since" comparison. The browser check holds the page and the build to the same count.
- **Profile text:** the heading over a stream's names planned to move comes from the profile (`keys_heading`), not the template. The template test now also checks for names from the OpenEMR profile.

## 0.4.8 (2026-10-21)

- **Every triage column sorts.** Before, only 6 of the 12 did, in one direction, with no visible cue.
  - **Reverse:** a second click reverses the order.
  - **Ranked columns put what needs attention first:** failing CI, changes requested, a needed rebase, a version number that's taken.
  - **Cues:** headers show ▲/▼ for the active sort and a faint ⇅ for the others, and carry `aria-sort` for screen readers.
  - **Stated order:** the count line says the order, such as "sorted oldest first", since a preset's sort can be on a field with no column.
  - **Ties:** ties keep the longest-open PR first, so every order is the same on every load.
- **"Nothing blocking" leaves out PRs with changes requested by a maintainer:** the next move there is the author's. On OpenEMR that removed 4 of 80. A new filter, "No changes requested", does the same on its own.
- **The base branch:** the path forward names the profile's base branch instead of "master".

## 0.4.7 (2026-10-21)

- **At a glance:** a row of counts at the top of the `motion` page. Each one opens the list behind it:
  - **open PRs**;
  - **stand alone:** no code conflict, break, stack, same-issue duplicate, shared name or confirmed clash with another open PR, so their order doesn't matter;
  - **groups to decide:** PRs joined by those links, decided together;
  - **nothing blocking**;
  - **clashes.**

  On OpenEMR: 301 open, 218 standalone, 26 groups covering 75 PRs (the largest has 8), 80 with nothing blocking, 2 clashes.
- **What joins a group:** overlaps only in mechanical files (version files, migrations, lint baselines) don't. They need a rebase, not a decision, and with them the largest group grew to 20. PRs over the file limit were never compared pair by pair, so they never count as standing alone.
- **The groups list** shows each group's PRs, with the pair links that join them.
- **Cards:** a PR's card names its group and the PRs to decide it with.
- **Triage:** two new filters, "Stands alone" and "In a group".
- **Tests:** a fixture test checks the groups against known links, including that two PRs sharing only a version number stand alone.

## 0.4.6 (2026-10-21)

- **`motion` reads CI the way `health` does.** It reads each check on a PR's head commit:
  - checks matching the profile's `health_ignore` are left out (Codecov by default; OpenEMR also leaves out its summary job);
  - a cancelled run is not a failure.

  Before, it took GitHub's overall rollup, which counts both. On OpenEMR that marked 28 PRs as failing CI when only a coverage report or a cancelled run was red, and left them out of "Nothing blocking".
- **Cards name the failing checks:** "CI failing on the head commit: PHP 8.3 - nginx - mariadb 12.3.3 / api", not "CI failing".
- **When GitHub lists more than 100 checks for a commit,** the page falls back to GitHub's rollup rather than judge on part of them.
- **A test holds `motion` and `health` to the same reading.**

## 0.4.5 (2026-10-21)

The `motion` page, checked in a browser at desktop and phone widths and in dark mode, with these fixes:
- **Rebuild it, or check one card:** a new panel says how to rebuild the page from the same commits or from today's, with the install and run commands, what they need, and how to recheck one card's findings with git alone. The header names the pr-toolbox version that built the page and links to it.
- **Literal code on cards:** a PR whose version number the base already uses would have shown `(${esc(BASE)} already uses it)` instead of the sentence. A test now scans the page's script for any double-quoted string holding `${`.
- **Stacked PRs:** two stacked PRs that carry one version bump are no longer counted as a version collision.
- **Triage:**
  - the filter takes PR numbers;
  - zero counts show as 0, not blank;
  - titles get room;
  - "Nothing blocking" lists the longest-open PRs first, not the most recently pushed.
- **Plurals:** "1 PR", not "1 PRs".
- **Phones:** long file paths wrap instead of widening the page.
- **Project text:** the template names no project's PRs or labels. Left-out labels and the large-PR limit come from the profile, and a test checks this.

## 0.4.4 (2026-10-20)

New command: **`pr-toolbox trailers`**. It rewrites commit trailers on your own open PRs, for example `Co-Authored-By: Claude …` to the `Assisted-by: Claude Code` a project asks for.
- **What changes:** only messages, and the parent lines that follow from them. Trees, authors, committers and dates are kept byte for byte.
- **The check:** before anything is pushed, the new branch must have the same commits, each with its old tree, author and committer, and no matching trailer left.
- **Safety:** a dry run unless `--push`, which pushes with a lease on the head it read. It works only on PRs you opened, and refuses signed commits.
- **Rules:** from `--remove` and `--replace`, or the profile's new `trailers` section.
- **`check`:** without `--forbid-trailer`, its `trailers` step forbids what the profile's `trailers` rules remove.
- **README:** the OpenEMR example now matches `Assisted-By` as well as `Assisted-by`, as OpenEMR's `CONTRIBUTING.md` writes it. It no longer presents a ban on `Co-Authored-By` as an OpenEMR rule, because OpenEMR's documents don't make one.

## 0.4.3 (2026-10-20)

- A [Claude Code](https://claude.com/claude-code) skill for `motion`: [`skills/pr-toolbox-motion/SKILL.md`](skills/pr-toolbox-motion/SKILL.md). It covers building the map, reading candidate pairs for semantic clashes, writing a project profile, and changing `motion` itself, with the rules and the traps met building it.
- A test checks that every `pr-toolbox motion` subcommand the skill names exists.

No code changed.

## 0.4.2 (2026-10-20)

- `pr-toolbox motion packets` now records each pair's heads in `semantic/heads.json`. Before, the page needed that file written by hand, and failed without it once verdicts existed.
- The page now builds when that file is missing, showing no reading as stale.
- A test covers both, and that packets withhold each PR's mention of its partner.

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

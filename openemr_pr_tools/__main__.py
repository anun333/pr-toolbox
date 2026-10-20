# SPDX-License-Identifier: GPL-3.0-or-later
"""pr-tools landing|health|check [options] (also installed as openemr-pr-tools)"""

import sys

from . import __version__, landing_order, pr_check, pr_health

COMMANDS = {"landing": landing_order.main, "health": pr_health.main, "check": pr_check.main}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(f"pr-tools {__version__}\n\nusage: pr-tools {{landing,health,check}} [options]\n\n"
              "  landing  which open PRs each of your PRs conflicts with, and its landing-order line\n"
              "  health   what changed on your open PRs: CI, conflicts with the base branch, review threads, merges\n"
              "  check    what your project's CI will say about your branch, before you push\n\n"
              "The repository comes from the current checkout (or --repo, --github-repo). Projects with a built-in\n"
              "profile (openemr/openemr) get its rules and checks; others can add a .pr-tools.json.\n"
              "Run a command with --help for its options.")
        return 0
    if sys.argv[1] == "--version":
        print(__version__)
        return 0
    command = COMMANDS.get(sys.argv[1])
    if command is None:
        print(f"unknown command {sys.argv[1]!r}; try landing, health or check", file=sys.stderr)
        return 2
    return command(sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())

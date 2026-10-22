# SPDX-License-Identifier: GPL-3.0-or-later
"""pr-toolbox motion: a map of a repository's open pull requests for its maintainers and contributors.

Every open PR merged onto the base and against every other PR that shares a file; breaks that merge cleanly as text;
the maintainers' plan per area, quoted from their own documents; a path forward; a triage table; and, optionally, a
reading of candidate pairs for semantic clashes. Writes a self-contained HTML page. Reads git objects and GitHub
records as data; runs nothing from any PR, and posts nothing anywhere.

    pr-toolbox motion [build] [--repo DIR] [--cache DIR] [--out FILE] [--threads N] [--skip-fetch]
    pr-toolbox motion candidates [--to-read N]     pairs worth reading for semantic clashes
    pr-toolbox motion packets                      one reading packet per chosen pair
    pr-toolbox motion compare A B                  two findings files for the same commits must agree
    pr-toolbox motion check-page [PAGE]            open the page in a headless browser and check what a reader meets
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import __version__, common

HERE = Path(__file__).resolve().parent
PRESETS = HERE.parent / "presets"

# Neutral defaults: what holds for any repository. A profile's "motion" section overrides them.
DEFAULTS = {
    "title": None,                      # "<Project> Work in Motion"
    "project": None,                    # the repository's name
    "mass_files": 150,                  # PRs changing more files are listed, not compared pair by pair
    "mechanical": None,                 # files every schema or lint change touches; derived from the profile when absent
    "mechanical_note": "version files, migrations and lint baselines",
    "release_tags": {"pattern": r"^v?(\d+)\.(\d+)\.0$", "count": 5},
    "skip_labels": [],                  # PRs and issues with these labels are left out of the areas
    "placeholder_issues": [],           # issue numbers a PR template uses as an example ("Fixes #12345")
    "left_out_paths": [],               # paths never listed as work to pick up
    "left_out_re": "$^",                # paths, classes and methods left out of the semantic candidates
    "left_out_note": "",
    "maintainer_roles": ["OWNER", "MEMBER"],
    "bots": r"(\[bot\]$|-bot$|^dependabot$)",
    "code_files": ["*", ":!vendor", ":!*/vendor/*", ":!node_modules", ":!tests"],
    "diff_files": None,                 # files whose diffs count retired patterns (default: code_files)
    "reach_files": None,                # files where callers of a tracked file are counted (default: code_files)
    "symbols": None,                    # "php" reads PHP symbols with PHP's tokenizer; None skips the symbol checks
    "php_image": "php:8.3-cli",
    "name_paths": [],
    "names": [],                        # shared-name kinds: {"kind", "where": {...}, "pattern", "format", "params"}
    "no_bump_re": None,
    "upgrade_files": None,
    "anchors": {"resources_dir": None, "resource_prefix": "", "resource_files_re": None, "schema_file": None,
                "route_re": None, "test_re": r"^(tests?/|.*/tests?/)", "infra_re": r"(?i)(Logger|getInstance|Helper)",
                "common_methods": ["get", "set", "save", "render", "execute", "insert", "update", "delete", "search"]},
    "invited_labels": ["help wanted", "help-wanted", "good first issue"],
    "streams": None,                    # areas, quoted targets, plan issues, retired patterns (a file or an object)
    "controls": None,                   # known cases each check must find (a file or a list)
    "conformance": None,                # outside standards each PR touches: specs, their paths and tools, controls (a file or an object)
    "method_notes": [],
    "release_symbols": None,            # {"functions": [...], "classes": [...], "pages_re": ...}: what each release removed
}


def resolve_profile(target: common.Target, args: argparse.Namespace) -> dict:
    """The profile every stage reads: the repository, its rules, and the motion settings, resolved to one object."""
    motion = {**DEFAULTS, **(target.config.get("motion") or {})}
    motion["anchors"] = {**DEFAULTS["anchors"], **((target.config.get("motion") or {}).get("anchors") or {})}
    owner, name = target.github.split("/")
    motion["project"] = motion["project"] or name
    motion["title"] = motion["title"] or f"{motion['project']} Work in Motion"
    for key in ("streams", "controls", "conformance"):
        value = motion[key]
        if isinstance(value, str):  # a file: next to the built-in presets, or relative to the checkout
            path = PRESETS / value if (PRESETS / value).exists() else target.repo / value
            value = json.loads(path.read_text())
        motion[key] = value
    if motion["streams"] is None:
        motion["streams"] = {"general": [], "rules_package": None, "streams": []}
    if motion["controls"] is None:
        motion["controls"] = {"controls": []}
    if motion["conformance"] is None:
        motion["conformance"] = {"specs": [], "process": [], "controls": []}
    vc, caps = target.config.get("version_claims"), target.config.get("exact_caps")
    if motion["mechanical"] is None:
        # files the version-claim and caps rules name are the mechanical ones
        globs = list((vc or {}).get("related") or ([vc["file"]] if vc else []))
        if caps:
            globs += [caps["file"], caps["counted"].replace("{name}", "*")]
        motion["mechanical"] = "^(" + "|".join(_glob_re(g) for g in globs) + ")$" if globs else "$^"
    return {"github": target.github, "owner": owner, "name": name, "remote": target.remote,
            "base_branch": target.base_branch, "version_claims": vc, "exact_caps": caps,
            "tool_version": __version__, "preset": getattr(args, "preset", None),
            "ci_ignore": target.config.get("health_ignore") or r"^codecov/", **motion}


def _glob_re(glob: str) -> str:
    out = ""
    for ch in glob:
        out += ".*" if ch == "*" else "[^/]" if ch == "?" else ("\\" + ch if ch in ".^$+{}[]()|\\" else ch)
    return out


def _run(cmd: list[str], threads: int | None, out: Path | None = None) -> None:
    if threads and shutil.which("taskset"):
        cmd = ["taskset", "-c", f"0-{threads - 1}", *cmd]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10", *cmd]
    with open(out, "w") if out else open(os.devnull, "w") as fh:
        r = subprocess.run(cmd, stdout=fh if out else None, env=os.environ)
    if r.returncode:
        raise SystemExit(r.returncode)


def build(target: common.Target, args: argparse.Namespace) -> int:
    cache = Path(args.cache).expanduser()
    (cache / "audit").mkdir(parents=True, exist_ok=True)
    out = Path(args.out).expanduser() if args.out else cache / "work-in-motion.html"
    profile = resolve_profile(target, args)
    (cache / "profile.json").write_text(json.dumps(profile, indent=1))
    os.environ["WIM_PROFILE"] = str(cache / "profile.json")
    os.environ["WIM_THREADS"] = str(max(1, args.threads))
    if args.master:
        os.environ["WIM_MASTER"] = args.master
    py, repo, threads, start = sys.executable, str(target.repo), args.threads, time.time()
    if profile["symbols"] == "php" and not shutil.which("php") and not shutil.which("docker"):
        print("PHP symbols need php or docker; the symbol checks will be skipped", file=sys.stderr)
        profile["symbols"] = None
        (cache / "profile.json").write_text(json.dumps(profile, indent=1))
    if not args.skip_fetch:
        print("1/4 fetch: open PRs, their heads, plan issues", flush=True)
        _run([py, "-I", str(HERE / "fetch.py"), repo, str(cache)], threads, cache / "fetch-summary.json")
    print("2/4 audit: each PR onto the base, every pair sharing a file, names, versions, caps", flush=True)
    _run([py, "-I", str(HERE / "repo_audit.py"), repo, str(cache / "audit"), str(cache / "open-prs.txt")], threads, cache / "audit-summary.json")
    print("3/4 controls", flush=True)
    (cache / "controls.json").write_text(json.dumps(profile["controls"], indent=1))
    _run([py, "-I", str(HERE / "controls.py"), str(cache / "audit" / "findings.json"), str(cache / "open-prs.txt"),
          str(cache / "controls.json"), str(cache / "audit" / "controls-result.json")], None)
    print("4/4 map and page", flush=True)
    _run([py, "-I", str(HERE / "intent.py"), repo, str(cache / "audit"), str(cache), str(HERE / "page.html"), str(out)], threads,
         cache / "map-summary.json")
    print(f"done in {time.time() - start:.0f}s: {out}")
    return 0


PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright/python:v1.63.0-noble"


def check_page(argv: list[str]) -> int:
    """Runs page_check.py on a built page: with this Python's Playwright if it has one, else in Playwright's image."""
    parser = argparse.ArgumentParser(prog="pr-toolbox motion check-page",
                                     description="Open a built page in a headless browser and check what a reader meets.")
    parser.add_argument("page", nargs="?", help="the page (default: <cache>/work-in-motion.html)")
    parser.add_argument("--cache", default=os.environ.get("WIM_CACHE", "~/.cache/pr-toolbox-motion"))
    parser.add_argument("--shots", help="also save each view's first screen here")
    parser.add_argument("--threads", type=int, default=3, help="CPUs for the Docker run (default: 3)")
    args = parser.parse_args(argv)
    page = Path(args.page or Path(args.cache) / "work-in-motion.html").expanduser().resolve()
    if not page.is_file():
        print(f"no page at {page}; build one first, or name it", file=sys.stderr)
        return 2
    shots = Path(args.shots).expanduser().resolve() if args.shots else None
    script = HERE / "page_check.py"
    if importlib.util.find_spec("playwright"):
        return subprocess.run([sys.executable, str(script), str(page), *(["--shots", str(shots)] if shots else [])]).returncode
    if shutil.which("docker"):
        mounts = ["-v", f"{page}:/page/index.html:ro", "-v", f"{script}:/opt/check/page_check.py:ro"]
        if shots:
            shots.mkdir(parents=True, exist_ok=True)
            mounts += ["-v", f"{shots}:/shots"]
        inner = ("pip install -q --break-system-packages playwright==1.63.0 >/dev/null 2>&1; "
                 "python3 /opt/check/page_check.py /page/index.html" + (" --shots /shots" if shots else ""))
        return subprocess.run(["docker", "run", "--rm", f"--cpus={args.threads}", "--ipc=host", "-w", "/tmp", *mounts,
                               PLAYWRIGHT_IMAGE, "bash", "-c", inner]).returncode
    print("check-page needs Playwright for Python (pip install playwright, then playwright install chromium) or Docker",
          file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    action = argv[0] if argv and argv[0] in {"build", "candidates", "packets", "compare", "check-page"} else "build"
    rest = argv[1:] if argv and argv[0] == action else argv
    if action == "check-page":
        return check_page(rest)
    if action == "compare":
        if len(rest) != 2:
            print("usage: pr-toolbox motion compare <findings a> <findings b>", file=sys.stderr)
            return 2
        return subprocess.run([sys.executable, "-I", str(HERE / "compare_findings.py"), *rest]).returncode
    parser = argparse.ArgumentParser(prog=f"pr-toolbox motion {action}", description=__doc__.split("\n\n")[0])
    common.add_target_arguments(parser)
    parser.add_argument("--cache", default=os.environ.get("WIM_CACHE", "~/.cache/pr-toolbox-motion"),
                        help="where inputs, caches and the page go (default: ~/.cache/pr-toolbox-motion)")
    parser.add_argument("--out", help="the page (default: <cache>/work-in-motion.html)")
    parser.add_argument("--threads", type=int, default=4, help="CPU threads to use, niced (default: 4)")
    parser.add_argument("--master", help="pin the base to a commit, e.g. to replay a build")
    parser.add_argument("--skip-fetch", action="store_true", help="reuse the cache's fetched inputs as they are")
    parser.add_argument("--to-read", type=int, default=40, help="candidates: how many pairs to choose for reading")
    args = parser.parse_args(rest)
    target = common.resolve(args)
    if action == "build":
        return build(target, args)
    cache = Path(args.cache).expanduser()
    profile = resolve_profile(target, args)
    (cache / "profile.json").write_text(json.dumps(profile, indent=1))
    os.environ["WIM_PROFILE"] = str(cache / "profile.json")
    if action == "candidates":
        _run([sys.executable, "-I", str(HERE / "semantic_candidates.py"), str(target.repo), str(cache), str(args.to_read)],
             args.threads, cache / "candidates.json")
        print(f"candidates: {cache / 'candidates.json'}")
        return 0
    _run([sys.executable, "-I", str(HERE / "semantic_packets.py"), str(target.repo), str(cache)], args.threads)
    print(f"packets: {cache / 'semantic' / 'packets'}; reader instructions: {HERE / 'semantic_reader_prompt.md'}")
    return 0

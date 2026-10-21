"""Checks the audit against known cases before anything is published: each control must be found (or, for an
"absent" control, must not be). Exits non-zero when a control whose PRs are all still open comes back wrong,
because then the check that should have found it is broken, and its zeros mean nothing.

    python3 -I controls.py <findings.json> <open-prs.txt> <controls.json> [results.json]
Writes each control's outcome to results.json when given, so the page can show what this build found.
"""
import json
import sys
from pathlib import Path

findings = json.loads(Path(sys.argv[1]).read_text())
open_prs = {int(l.split()[0]) for l in Path(sys.argv[2]).read_text().splitlines() if l.strip()}
controls = json.loads(Path(sys.argv[3]).read_text())["controls"]

failed = 0
results = []
for c in controls:
    if not set(c["prs"]) <= open_prs:
        print(f"  RETIRED {c['name']} (not all of {c['prs']} are open)")
        results.append({"name": c["name"], "outcome": "retired"})
        continue
    if "key" in c:  # membership: the PR is a key of the dict at a dotted path, e.g. stale_upgrade.prs
        node = findings
        for part in c["in"].split("."):
            node = node.get(part, {})
        ok = str(c["key"]) in {str(k) for k in node}
        print(f"  {'FOUND ' if ok else 'VOID  '} {c['name']}")
        results.append({"name": c["name"], "outcome": "found" if ok else "void"})
        failed += not ok
        continue
    hits = [x for x in findings.get(c["in"], []) if all(x.get(k) == v for k, v in c["match"].items())]
    if "contains" in c:
        hits = [x for x in hits if set(c["contains"]) <= set(x.get("prs", []))]
    ok = (not hits) if c.get("expect") == "absent" else bool(hits)
    print(f"  {'FOUND ' if ok and c.get('expect') != 'absent' else 'ABSENT' if ok else 'VOID  '} {c['name']}")
    results.append({"name": c["name"], "outcome": ("absent, as it must be" if c.get("expect") == "absent" else "found") if ok else "void"})
    failed += not ok
if len(sys.argv) > 4:
    Path(sys.argv[4]).write_text(json.dumps(results, indent=1))
if failed:
    sys.exit(f"{failed} control(s) wrong: the checks behind them can't be trusted on this run")

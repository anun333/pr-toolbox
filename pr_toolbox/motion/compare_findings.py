"""Two findings.json files for the same master and PR heads must say the same thing. Used to prove an incremental
rebuild equals a full one. Lists are compared as sets of JSON rows, since reused and recomputed results can come
out in a different order; the summary's incremental counters are left out.

    python3 -I compare_findings.py <findings a> <findings b>
"""
import json
import sys

a, b = (json.load(open(p)) for p in sys.argv[1:3])
diffs = 0


def norm(v):
    if isinstance(v, list):
        return sorted(json.dumps(x, sort_keys=True) for x in v)
    if isinstance(v, dict):
        return {k: norm(x) for k, x in v.items()}
    return v


for key in sorted(set(a) | set(b)):
    if key == "summary":
        sa = {k: v for k, v in a[key].items() if k != "incremental"}
        sb = {k: v for k, v in b[key].items() if k != "incremental"}
        same = norm(sa) == norm(sb)
    else:
        same = norm(a.get(key)) == norm(b.get(key))
    n = len(a.get(key) or []) if not isinstance(a.get(key), dict) else len(a.get(key))
    print(f"{'same   ' if same else 'DIFFERS'} {key} ({n})")
    if not same:
        diffs += 1
        if isinstance(a.get(key), list):
            sa, sb = set(norm(a[key])), set(norm(b.get(key) or []))
            for row in sorted(sa - sb)[:5]:
                print("   only in a:", row[:200])
            for row in sorted(sb - sa)[:5]:
                print("   only in b:", row[:200])
sys.exit(1 if diffs else 0)

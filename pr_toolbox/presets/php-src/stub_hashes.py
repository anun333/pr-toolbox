#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Each stub's generated headers must carry the stub's current hash, as php-src's CI checks by regenerating them:
build/gen_stub.php writes "Stub hash: <sha1 of the stub, CRLF read as LF>" into <name>_arginfo.h, and into
<name>_decl.h when the stub has one. Reads only; to regenerate, run build/gen_stub.php <stub> (it needs php).

    python3 -I stub_hashes.py <stub.php>...
"""
import hashlib
import re
import sys
from pathlib import Path

HASH = re.compile(rb"\* Stub hash: ([0-9a-f]{40})")
stale, checked = [], 0
for name in sys.argv[1:]:
    stub = Path(name)
    if not stub.name.endswith(".stub.php") or not stub.exists():  # a stub the branch deletes has nothing to check
        continue
    checked += 1
    want = hashlib.sha1(stub.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    base = str(stub)[: -len(".stub.php")]
    arginfo = Path(base + "_arginfo.h")
    if not arginfo.exists():
        stale.append(f"{stub}: no {arginfo.name} beside it")
    for header in (arginfo, Path(base + "_decl.h")):
        if not header.exists():
            continue
        got = HASH.search(header.read_bytes())
        if not got or got.group(1).decode() != want:
            stale.append(f"{header}: stub hash {got.group(1).decode()[:10] if got else 'missing'}, the stub is {want[:10]};"
                         f" run build/gen_stub.php {stub}")
noun = "stub's" if checked == 1 else "stubs'"
print("\n".join(stale) if stale else f"{checked} {noun} generated headers are current")
sys.exit(1 if stale else 0)

# SPDX-License-Identifier: GPL-3.0-or-later
"""Open a built work-in-motion page in a headless browser and check what a reader meets, at desktop and phone widths
and in dark mode: script errors, sideways scrolling, empty sections, links to nowhere, code printed as text, and
whether the counts, filters, groups, sorting, folded sections and cards do what they say. Each check prints what it
saw; the exit code is 1 if any fails. Needs Playwright for Python and its Chromium; `pr-toolbox motion check-page`
runs it locally or in Playwright's Docker image.

    python3 page_check.py <page.html> [--shots DIR]
"""

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PAGE = Path(sys.argv[1]).resolve()
SHOTS = Path(sys.argv[sys.argv.index("--shots") + 1]) if "--shots" in sys.argv else None
URL = PAGE.as_uri()
MAP = json.loads(re.search(r"const MAP = (\{.*?\});\n", PAGE.read_text()).group(1))
PRS = MAP["prs"]
CONTAINERS = ["stamp", "glance-row", "path-body", "presets", "filters", "ttable", "general", "area-list", "twice-body", "queues-body",
              "merge-stats", "merge-body", "release-table", "method-body"]
CODE_AS_TEXT = ["${", "[object Object]", "esc(", " 1 PRs "]
fails = []


def check(name, ok, seen=""):
    print(("PASS " if ok else "FAIL ") + name + (": " + str(seen)[:300] if seen != "" else ""))
    if not ok:
        fails.append(name)


def count_shown(pg):
    """The number before "of" in the triage count, e.g. "76 of 307 open PRs · sorted oldest first"."""
    return int(pg.inner_text("#tcount").split(" ")[0])


def col(pg, i):
    return pg.eval_on_selector_all(f"#ttable tbody tr[data-n] td:nth-child({i})", "c => c.map(x => x.innerText)")


def press(pg, selector, wait=250):
    pg.click(selector)
    pg.wait_for_timeout(wait)


def everywhere(pg, label, width):
    """What must hold at every width and in every colour scheme."""
    sw = pg.evaluate("document.documentElement.scrollWidth")
    check(f"{label}: no sideways scroll", sw <= width, f"page {sw}px wide")
    wide = pg.evaluate("""(vw) => [...document.querySelectorAll('body *')].filter(el => { const r = el.getBoundingClientRect();
        return r.width && r.right > vw + 1 && !el.closest('.scroll, pre, table'); }).slice(0, 3).map(el => el.tagName + ' ' + (el.innerText || '').slice(0, 40))""", width)
    check(f"{label}: nothing sticks out past the screen", not wide, wide)
    missing = pg.evaluate("""() => [...document.querySelectorAll('a[href^="#"]')].map(a => a.getAttribute('href').slice(1))
        .filter(id => id && !document.getElementById(decodeURIComponent(id)))""")
    check(f"{label}: every in-page link has a target", not missing, sorted(set(missing))[:5])
    empty = [c for c in CONTAINERS if pg.query_selector(f"#{c}") and not pg.inner_text(f"#{c}").strip()
             and not pg.eval_on_selector(f"#{c}", "e => e.closest('[hidden]') !== null")]
    check(f"{label}: no empty section", not empty, empty)
    text = pg.inner_text("body")
    printed = {w: text.count(w) for w in CODE_AS_TEXT if w in text}
    check(f"{label}: no code printed as text", not printed, printed)


def desktop(pg):
    g = MAP.get("glance") or {}
    # the summary strip, first
    box = pg.query_selector("#glance-row")
    top = box.bounding_box()["y"] if box else None
    check("summary strip on the first screen", top is not None and top + 120 < 900, f"top at {top}px")
    tiles = dict(pg.eval_on_selector_all("#glance-row button", "bs => bs.map(b => [b.dataset.glance, +b.querySelector('.big').innerText])"))
    check("strip counts match the build", tiles.get("all") == g.get("open") and tiles.get("alone") == g.get("alone") and tiles.get("groups") == g.get("groups"), tiles)
    if "shortlist" in next(iter(PRS.values()), {}):
        check("page and build agree on Nothing blocking", tiles.get("shortlist") == sum(1 for p in PRS.values() if p.get("shortlist")),
              f"page {tiles.get('shortlist')}, build {sum(1 for p in PRS.values() if p.get('shortlist'))}")
    press(pg, '#glance-row [data-glance="alone"]')
    check("stand alone filters triage to that many", count_shown(pg) == g.get("alone"), pg.inner_text("#tcount"))
    press(pg, '#glance-row [data-glance="groups"]')
    groups = pg.eval_on_selector_all("#groups-body .group", "gs => gs.map(g => g.querySelectorAll('.gnode').length)")
    check("groups list opens with every group", pg.eval_on_selector("#groups-list", "d => d.open") and len(groups) == g.get("groups"), f"{len(groups)} groups")
    check("each group is drawn with all its PRs", groups == [len(x["prs"]) for x in MAP.get("groups", [])], groups[:6])
    if pg.query_selector("#groups-body .gnode"):
        n = pg.eval_on_selector("#groups-body .gnode", "g => g.dataset.n")
        press(pg, f'#groups-body .gnode[data-n="{n}"]', 400)
        check("selecting a circle opens its card", pg.inner_text("#result").startswith(f"#{n}"), pg.inner_text("#result")[:60])
    # the shortlist
    for b in pg.query_selector_all("#presets button"):
        if b.inner_text() == "Nothing blocking":
            b.click()
    pg.wait_for_timeout(250)
    if pg.query_selector("#tall"):
        press(pg, "#tall")
    ages = [int(a) for a in col(pg, 3)]
    check("Nothing blocking lists the oldest first", ages == sorted(ages, reverse=True), ages[:5])
    asked = [r for r in col(pg, 5) if "CHANGES REQUESTED" in r.upper()]
    check("Nothing blocking has no changes requested", not asked, f"{len(asked)} rows")
    # all PRs: 25 rows, then all; every column sorts both ways
    for b in pg.query_selector_all("#presets button"):
        if b.inner_text().startswith("Clear"):
            b.click()
    pg.wait_for_timeout(250)
    rows = pg.eval_on_selector_all("#ttable tbody tr[data-n]", "rs => rs.length")
    check("triage shows 25 rows, then a button for all", rows == min(25, len(PRS)) and (len(PRS) <= 25 or pg.query_selector("#tall") is not None), f"{rows} rows")
    if pg.query_selector("#tall"):
        press(pg, "#tall")
    heads = pg.eval_on_selector_all("#ttable thead th", "ts => ts.map(t => [!!t.querySelector('button[data-sort]'), t.getAttribute('aria-sort')])")
    check("every column sorts and says so", heads and all(b and a in ("none", "ascending", "descending") for b, a in heads), f"{sum(b for b, _ in heads)} of {len(heads)}")
    # wait for the header to announce each order before reading rows: a fixed pause read php-src's 1,220-row table
    # before it had redrawn
    sorted_as = "d => document.querySelector('#ttable th button[data-sort=\"t\"]').parentElement.getAttribute('aria-sort') === d"
    # the title itself, not the cell's first line: pills follow it (draft, a release branch), and php-src opens one fix
    # under one title on two branches (#24102 on PHP-8.6, #24103 on master), which the pill would put out of order
    title_text = "tds => tds.map(td => td.firstChild.textContent.trim().toLowerCase())"
    press(pg, '#ttable th button[data-sort="t"]')
    pg.wait_for_function(sorted_as, arg="ascending", timeout=10000)
    titles = pg.eval_on_selector_all("#ttable tbody tr[data-n] td:nth-child(2)", title_text)
    press(pg, '#ttable th button[data-sort="t"]')
    pg.wait_for_function(sorted_as, arg="descending", timeout=10000)
    titles2 = pg.eval_on_selector_all("#ttable tbody tr[data-n] td:nth-child(2)", title_text)
    check("title sorts A to Z, then Z to A", titles == sorted(titles) and titles2 == sorted(titles2, reverse=True), titles[:2])
    press(pg, '#ttable th button[data-sort="age"]')
    ages, aria = [int(a) for a in col(pg, 3)], pg.eval_on_selector('#ttable th button[data-sort="age"]', "b => b.parentElement.getAttribute('aria-sort')")
    check("age sorts oldest first and announces it", ages == sorted(ages, reverse=True) and aria == "descending", f"{ages[:3]}, {aria}")
    check("the count says the order", "sorted" in pg.inner_text("#tcount"), pg.inner_text("#tcount"))
    pills = pg.eval_on_selector_all("#ttable tbody .pill", "ps => ps.map(p => p.innerText.toUpperCase())")
    ordinary = [x for x in pills if x in ("CI PASSING", "MERGES", "NO MAINTAINER REVIEW")]
    check("triage marks only what is out of the ordinary", not ordinary, f"{len(ordinary)} pills for ordinary states")
    titled = pg.eval_on_selector_all('#ttable tbody a[href*="/pull/"]', "as => as.filter(a => a.title).length")
    check("PR numbers show their titles on hover", titled == pg.eval_on_selector_all("#ttable tbody tr[data-n]", "rs => rs.length"), f"{titled} titled")
    first = next(iter(PRS))
    pg.fill("#tq", first)
    pg.wait_for_timeout(250)
    check("triage filters by PR number", pg.eval_on_selector_all("#ttable tbody tr[data-n]", "rs => rs.map(r => r.dataset.n)") == [first], first)
    pg.fill("#tq", "")
    # the lookup: a PR and a folder
    pg.fill("#q", first)
    press(pg, "#lookup-form button", 300)
    check("a PR number opens its card", pg.inner_text("#result").startswith(f"#{first}"), pg.inner_text("#result")[:60])
    folder = next((f for f in MAP.get("files") or {} if "/" in f), "").rsplit("/", 1)[0]
    if folder:
        pg.fill("#q", folder + "/")
        press(pg, "#lookup-form button", 300)
        check("a folder lists the open PRs there", "open PR" in pg.inner_text("#result"), pg.inner_text("#result")[:80])
    # version numbers on cards: a number the base already uses is said in words; a stack's shared bump is no collision
    taken = next((n for n, p in PRS.items() if p.get("claim_taken")), None)
    if taken:
        pg.fill("#q", taken)
        press(pg, "#lookup-form button", 300)
        line = next((x for x in pg.inner_text("#result").splitlines() if "already uses it" in x), "")
        check("a card says when the base already uses its version number", bool(line), line or taken)
    # A stack whose two PRs make the same bump: alone on that number it is no collision; with an outside PR
    # on the number too, it is one, and the card names that PR (OpenEMR 2026-10-23: #14808 beside #14708/#14715).
    claimants = {}
    for number, pr in PRS.items():
        if pr.get("claim") is not None:
            claimants.setdefault(pr["claim"], set()).add(str(number))
    stacks = [x for x in (MAP.get("duplicates") or {}).get("stacked", [])
              if PRS.get(str(x["in"]), {}).get("claim") is not None and PRS.get(str(x["in"]), {}).get("claim") == PRS.get(str(x["contained"]), {}).get("claim")]
    outside = lambda x: claimants.get(PRS[str(x["in"])]["claim"], set()) - {str(x["in"]), str(x["contained"])}
    for stack in [next((x for x in stacks if not outside(x)), None), next((x for x in stacks if outside(x)), None)]:
        if stack is None:
            continue
        pg.fill("#q", str(stack["contained"]))
        press(pg, "#lookup-form button", 300)
        line = next((x for x in pg.inner_text("#result").splitlines() if "stacked with it" in x), "")
        if outside(stack):
            check("a stack sharing its bump with an outside PR is a collision, naming that PR",
                  "SHARED" in line.upper() and any(f"#{n}" in line for n in outside(stack)), line or stack)
        else:
            check("a stack's shared version bump is not called a collision", bool(line) and "SHARED" not in line.upper(), line or stack)
    # folded areas open when a link points into them
    folds = pg.eval_on_selector_all("#area-list details.fold", "ds => ds.map(d => d.open)")
    if MAP.get("streams"):
        check("areas start folded", folds and not any(folds), f"{len(folds)} areas, {sum(folds)} open")
        streamed = next((n for n, p in PRS.items() if p.get("streams")), None)
        if streamed:
            pg.fill("#q", streamed)
            press(pg, "#lookup-form button", 300)
            link = pg.query_selector('#result a[href^="#area-"]')
            target = link.get_attribute("href")[1:] if link else None
            if link:
                link.click()
                pg.wait_for_timeout(300)
            check("a link into a folded area opens it", bool(target) and pg.eval_on_selector(f"#{target}", "d => d.open"), target)
    # the conformance lens: a fold per standard, a matched PR's card names its standards, and the filter counts them
    conf = MAP.get("conformance")
    if conf:
        folds_c = pg.eval_on_selector_all("#conf-list details.fold.std", "ds => ds.length")
        check("conformance: a fold per standard", folds_c == len(conf["specs"]), f"{folds_c} folds, {len(conf['specs'])} standards")
        certified = {s["id"]: s for s in conf["specs"] if s.get("certified")}
        matched = next((n for n, p in PRS.items() if set(p.get("conf") or []) & set(certified)), None)
        if matched:
            pg.fill("#q", matched)
            press(pg, "#lookup-form button", 300)
            card = pg.inner_text("#result")
            labels = [certified[i]["label"] for i in PRS[matched]["conf"] if i in certified]
            # the panel heading is styled in capitals, and inner_text returns it as rendered
            check("conformance: a matched PR's card names its standards", "standards" in card.lower() and all(x in card for x in labels), f"#{matched}: {labels}")
            link = pg.query_selector('#result a[href^="#conf-"]')
            target = link.get_attribute("href")[1:] if link else None
            if link:
                link.click()
                pg.wait_for_timeout(300)
            check("conformance: a link from a card opens the standard", bool(target) and pg.eval_on_selector(f"#{target}", "d => d.open"), target)
        # gaps: every standard says what was measured; the untested count matches the build
        blocks = pg.eval_on_selector_all("#conf-list details.fold.std", "ds => ds.filter(d => d.textContent.includes('official test in CI')).length")  # textContent: a closed fold hides its body from innerText
        check("conformance: every standard says whether its official test runs in CI", blocks == len(conf["specs"]), f"{blocks} of {len(conf['specs'])}")
        untested = sum(1 for s in conf["specs"] if s.get("certified") and not (s.get("measured") or {}).get("ci"))
        index = pg.inner_text("#conf-index summary")
        check("conformance: the summary counts the certified standards with no official test in CI", f"{untested} of the {len(certified)} with no official test in CI" in index, index[:160])
        want = sum(1 for p in PRS.values() if set(p.get("conf") or []) & set(certified))
        for b in pg.query_selector_all("#presets button"):
            if b.inner_text().startswith("Clear"):
                b.click()
        press(pg, "#f-certified")
        check("conformance: the filter shows every PR touching a certified criterion", count_shown(pg) == want, f"page {count_shown(pg)}, build {want}")
        press(pg, "#f-certified")
    if not MAP.get("streams"):
        text = pg.inner_text("#area-list")
        check("with no areas, the section says the profile names none", text.startswith("No areas yet"), text[:80])
    # open PRs aimed at another branch are named under the limits, and neither compared nor listed as over the file limit
    others = [str(o["number"]) for o in MAP.get("other_bases", [])]
    named = pg.eval_on_selector("#other-bases", "e => e.textContent") if others and pg.query_selector("#other-bases") else ""
    stray = [n for n in others if n in PRS or n in MAP["stamp"]["mass"]]
    check("PRs aimed at another branch are named, not compared", "other_bases" in MAP and all(f"#{n}" in named for n in others) and not stray,
          f"{len(others)} named; compared anyway: {stray}" if "other_bases" in MAP else "the page doesn't say which PRs aim at another branch")
    # merge-up (php-src): a PR aimed at a release branch names it beside its title, and each conflict further up the
    # chain has its own line on its card. Lower case throughout: pills may display in capitals.
    up = next((n for n, p in PRS.items() if p.get("merge_up")), None)
    if up:
        pg.fill("#q", up)
        press(pg, "#lookup-form button", 300)
        card = pg.inner_text("#result").lower()
        first = PRS[up]["merge_up"][0]["branch"].lower()
        check("a release-branch PR's card names its branch and its merge-up conflicts",
              PRS[up]["base"].lower() in card.splitlines()[0] and "merge-up conflict" in card and f"into {first}" in card, card[:140])
    # queues: a count lists exactly that many PRs in triage, says which, and clearing it brings the rest back
    fold = pg.query_selector("#queues-fold")
    check("queues: the counts fold closed by default", fold is None or not pg.eval_on_selector("#queues-fold", "d => d.open"), "open" if fold and pg.eval_on_selector("#queues-fold", "d => d.open") else "closed")
    if fold:
        pg.eval_on_selector("#queues-fold", "d => { d.open = true; }")
    cells = pg.eval_on_selector_all("#queues-body button[data-qk]", "bs => bs.map(b => [b.dataset.qk, b.dataset.qv, b.dataset.qs, +b.textContent])")
    check("queues: counts to select", bool(cells) or not PRS, f"{len(cells)} counts")
    if cells:
        kind, value, state, want = max(cells, key=lambda c: (c[2] == "unreviewed", c[3] < 200, c[3]))
        press(pg, f'#queues-body button[data-qk="{kind}"][data-qv="{value}"][data-qs="{state}"]', 400)
        shown, scope = count_shown(pg), pg.inner_text("#tscope") if pg.query_selector("#tscope") else ""
        check("queues: a count lists that many PRs in triage, and says which", shown == want and bool(scope), f"{kind} {value} {state}: count {want}, triage {shown}, scope {scope!r}")
        press(pg, "#tscope-clear", 400)
        check("queues: clearing the scope brings the rest back", pg.query_selector("#tscope") is None and count_shown(pg) > want or count_shown(pg) == len(PRS),
              pg.inner_text("#tcount")[:80])
    has_owners = any(p.get("owners") for p in PRS.values())
    check("queues: an owner table when CODEOWNERS names someone", (pg.query_selector('[data-queue="owner"]') is not None) == has_owners, f"owners in the data: {has_owners}")
    twins = (MAP.get("duplicates") or {}).get("same_title") or []
    if twins:
        twice = pg.inner_text("#twice-body").split("One fix, two PRs".upper() if "ONE FIX, TWO PRS" in pg.inner_text("#twice-body") else "One fix, two PRs")[-1]
        missing = [t["prs"] for t in twins if not all(f"#{n}" in twice for n in t["prs"])]
        check("same work twice: every fix opened twice is named", not missing, missing[:3])
    if MAP.get("exports"):
        hrefs = pg.eval_on_selector_all("#data a[download]", "as => as.map(a => a.getAttribute('href'))")
        files = sorted(MAP["exports"].values())
        check("the data files are linked and sit beside the page", sorted(hrefs) == files and all((PAGE.parent / f).exists() for f in files), f"{hrefs} / {files}")
    gaps = (MAP.get("labels_vs_build") or {}).get("gaps")
    if (MAP.get("labels_vs_build") or {}).get("labels"):
        listed = pg.eval_on_selector_all("#label-gaps li.prline", "ls => ls.length")
        check("labels the build disagrees with: each one listed", listed == len(gaps), f"page {listed}, build {len(gaps)}")
    # a code conflict found only head to head ("stale_conflicts": "apart") has its own line, not the mechanical one
    later = next((n for n, p in PRS.items() if any(c["kind"] == "after a rebase" for c in p.get("conflicts", []))), None)
    if later:
        pg.fill("#q", later)
        press(pg, "#lookup-form button", 300)
        lines = pg.inner_text("#result").lower().splitlines()
        line = next((x for x in lines if x.startswith("after a rebase")), "")
        mech_line = next((x for x in lines if x.startswith("mechanical")), "")
        partners = [str(c["n"]) for c in PRS[later]["conflicts"] if c["kind"] == "after a rebase"]
        check("a conflict found head to head has its own line, not the mechanical one",
              bool(line) and all(f"#{n}" in line for n in partners[:3]) and not any(f"#{n} " in mech_line + " " for n in partners), line[:120] or later)
    # a PR that left the list but is still open (now aimed at another branch) was neither merged nor closed
    s = MAP.get("since") or {}
    left = {x["n"] for k in ("merged", "closed", "gone") for x in s.get(k, [])}
    still_open = sorted(left & ({o["number"] for o in MAP.get("other_bases", [])} | {int(n) for n in PRS}))
    check("since the last build: no PR still open is called merged, closed or gone", not still_open, still_open)
    since = pg.inner_text("#since") if pg.query_selector("#since") else ""
    check("since the last build is shown", since.startswith("Since") or since.startswith("No earlier build"), since[:100])
    check("the header names the tool and how to rebuild", "pr-toolbox" in pg.inner_text("#stamp") and pg.query_selector("#rebuild") is not None)


with sync_playwright() as p:
    browser = p.chromium.launch()
    for label, width, height, scheme in [("desktop", 1280, 900, "light"), ("phone", 390, 844, "light"), ("dark", 1280, 900, "dark")]:
        pg = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme)
        errors = []
        pg.on("pageerror", lambda e, errors=errors: errors.append(str(e)[:200]))
        pg.on("console", lambda m, errors=errors: errors.append(m.text[:200]) if m.type == "error" else None)
        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_timeout(500)
        height_px = pg.evaluate("document.documentElement.scrollHeight")
        print(f"== {label}: {height_px}px tall, {height_px / height:.1f} screens")
        if SHOTS:
            SHOTS.mkdir(parents=True, exist_ok=True)
            pg.screenshot(path=str(SHOTS / f"{label}-top.png"))
        everywhere(pg, label, width)
        if label == "phone" and MAP.get("conformance"):
            # a closed fold hides its body from the overflow check above: open every standard and measure again
            pg.evaluate("document.querySelectorAll('#conf-list details').forEach(d => d.open = true)")
            pg.wait_for_timeout(300)
            wide = pg.evaluate("""(vw) => [...document.querySelectorAll('#conf-list *')].filter(el => { const r = el.getBoundingClientRect();
                return r.width && r.right > vw + 1 && !el.closest('.scroll, pre, table'); }).slice(0, 3).map(el => el.tagName + ' ' + (el.innerText || '').slice(0, 40))""", width)
            # a pill's text can spill past its own box (pills don't wrap), which no element's box shows: the page width does
            sw = pg.evaluate("document.documentElement.scrollWidth")
            check("phone: the opened standards fit the screen", not wide and sw <= width, f"page {sw}px wide; {wide}")
            pg.evaluate("document.querySelectorAll('#conf-list details').forEach(d => d.open = false)")
        if label == "phone":
            box = pg.query_selector("#glance-row")
            top = box.bounding_box()["y"] if box else None
            check("phone: summary strip on the first screen", top is not None and top + 80 < height, f"top at {top}px")
        if label == "desktop":
            desktop(pg)
        check(f"{label}: no script errors", not errors, errors[:3])
        pg.close()
    if MAP.get("exports"):
        # framed in the claude.ai viewer, a page can't follow a download link: the data links must save through the
        # viewer's downloads capability. A stand-in viewer records what the page asks to save.
        # served over HTTP, as the viewer serves it: a page opened as a file may not fetch its neighbours
        import functools
        import http.server
        import threading

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(PAGE.parent)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.add_init_script("""window.__saved = []; window.claude = { use: (name) => Promise.resolve(name === "downloads"
            ? { save: (r) => { window.__saved.push([r.filename, r.data && r.data.size]); return Promise.resolve({ status: "saved" }); } } : null) };""")
        pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/{PAGE.name}", wait_until="networkidle")
        pg.wait_for_timeout(800)
        pg.click('#data a[href$=".csv"]')
        pg.wait_for_timeout(800)
        saved = pg.evaluate("window.__saved")
        csv_name = MAP["exports"]["prs_csv"]
        check("in the viewer, a data link saves through the downloads capability",
              len(saved) == 1 and saved[0][0].endswith(csv_name) and (saved[0][1] or 0) == (PAGE.parent / csv_name).stat().st_size,
              saved or (pg.inner_text("#data-note") if pg.query_selector("#data-note") else "nothing saved, and no note on the page"))
        pg.close()
        srv.shutdown()
    browser.close()
print(f"{len(fails)} failed" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)

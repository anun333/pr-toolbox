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
CONTAINERS = ["stamp", "glance-row", "path-body", "presets", "filters", "ttable", "general", "area-list", "twice-body",
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
    press(pg, '#ttable th button[data-sort="t"]')
    titles = [t.splitlines()[0].lower() for t in col(pg, 2)]
    press(pg, '#ttable th button[data-sort="t"]')
    titles2 = [t.splitlines()[0].lower() for t in col(pg, 2)]
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
    stack = next((x for x in (MAP.get("duplicates") or {}).get("stacked", [])
                  if PRS.get(str(x["in"]), {}).get("claim") is not None and PRS.get(str(x["in"]), {}).get("claim") == PRS.get(str(x["contained"]), {}).get("claim")), None)
    if stack:
        pg.fill("#q", str(stack["contained"]))
        press(pg, "#lookup-form button", 300)
        line = next((x for x in pg.inner_text("#result").splitlines() if "stacked with it" in x), "")
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
        want = sum(1 for p in PRS.values() if set(p.get("conf") or []) & set(certified))
        for b in pg.query_selector_all("#presets button"):
            if b.inner_text().startswith("Clear"):
                b.click()
        press(pg, "#f-certified")
        check("conformance: the filter shows every PR touching a certified criterion", count_shown(pg) == want, f"page {count_shown(pg)}, build {want}")
        press(pg, "#f-certified")
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
        if label == "phone":
            box = pg.query_selector("#glance-row")
            top = box.bounding_box()["y"] if box else None
            check("phone: summary strip on the first screen", top is not None and top + 80 < height, f"top at {top}px")
        if label == "desktop":
            desktop(pg)
        check(f"{label}: no script errors", not errors, errors[:3])
        pg.close()
    browser.close()
print(f"{len(fails)} failed" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""
Grøn Koncert Resale Watcher (v4)
Overvåger Resale-markedspladsen for Odense, Næstved og Valby
og sender push-notifikation via ntfy.sh, når der kommer billetter til salg.

Detektion baseret på Billetten-widgettens faktiske ordlyd:
  - Tom markedsplads:  "Ingen resalebilletter tilgængelig pt."
  - Billetter til salg: "Køb Resale"-knap vises i widgetten

Kører automatisk via GitHub Actions (se .github/workflows/watch.yml).
"""

import hashlib
import json
import os
import re
import traceback
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ── Konfiguration ────────────────────────────────────────
URL = "https://groenkoncert.dk/billetter/"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "groen-ekkelund-billet-7391")
STATE_FILE = Path(__file__).parent / "groen_state.json"
DEBUG_DIR = Path(__file__).parent / "debug"

ALL_CITIES = ["Tårnby", "Kolding", "Aarhus", "Aalborg", "Esbjerg", "Odense", "Næstved", "Valby"]
WATCH_CITIES = ["Odense", "Næstved", "Valby"]

EMPTY_PHRASE = "ingen resalebillet"   # matcher "Ingen resalebilletter tilgængelig pt."
# ─────────────────────────────────────────────────────────


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slug(city: str) -> str:
    return city.lower().replace("æ", "ae").replace("ø", "oe").replace("å", "aa")


def notify(title: str, message: str, priority: str = "urgent") -> None:
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": "ticket,green_circle",
                "Click": URL,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[{ts()}] ntfy-fejl: {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def dump(name: str, content: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / name).write_text(content, encoding="utf-8")


def dismiss_cookie_banner(page) -> None:
    for attempt in range(3):
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        if "ACCEPTER ALLE" not in body.upper():
            return
        for finder in [
            lambda: page.get_by_role("button", name=re.compile("afvis alle", re.I)).first,
            lambda: page.get_by_role("button", name=re.compile("afvis", re.I)).first,
            lambda: page.locator("button:has-text('AFVIS')").first,
            lambda: page.locator("a:has-text('AFVIS ALLE')").first,
        ]:
            try:
                finder().click(timeout=2500)
                page.wait_for_timeout(1500)
                print(f"[{ts()}] Cookie-banner lukket")
                break
            except Exception:
                continue


def cities_on_page(page) -> list:
    body = page.inner_text("body")
    start = body.upper().find("KONCERTER")
    end = body.upper().find("DIVERSE")
    section = body[start:end] if 0 <= start < end else body
    found = []
    for c in ALL_CITIES:
        m = re.search(rf"(?m)^\s*\+?\s*{re.escape(c.upper())}\s*$", section.upper())
        if m:
            found.append((m.start(), c))
    found.sort()
    return [c for _, c in found]


def find_widget_text(context) -> str:
    """Find Billetten-widgettens tekst (iframe med 'GRØN - <BY>')."""
    best = ""
    for p in context.pages:
        for frame in p.frames:
            try:
                t = frame.inner_text("body")
            except Exception:
                continue
            if "GRØN -" in t.upper() and len(t) > len(best):
                best = t
    return best


def open_city_widget(context, page, city: str, present: list) -> str:
    """Åbn byens resale-widget og returnér widget-teksten. Verificerer at det er den rigtige by."""
    idx = present.index(city)

    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    dismiss_cookie_banner(page)

    buttons = page.locator("text=/Køb Resale/i")
    count = buttons.count()
    per_city = max(1, count // len(present))
    print(f"[{ts()}] {city}: {count} knapper, {len(present)} byer, {per_city} pr. by, indeks {idx}")

    if count == 0:
        raise RuntimeError("Ingen 'Køb Resale'-knapper fundet")

    # Prøv header-knappen først, panel-knappen som fallback
    candidates = [idx * per_city + o for o in range(per_city)]
    widget = ""
    for cand in candidates:
        if cand >= count:
            continue
        target = buttons.nth(cand)
        pages_before = len(context.pages)
        try:
            target.scroll_into_view_if_needed(timeout=8000)
            target.click(timeout=8000)
        except Exception as e:
            print(f"[{ts()}] {city}: klik på knap {cand} fejlede ({e})")
            continue
        page.wait_for_timeout(6000)
        widget = find_widget_text(context)

        # Ryd op: luk nye faner og modal
        while len(context.pages) > pages_before:
            try:
                context.pages[-1].close()
            except Exception:
                break
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        if city.upper() in widget.upper():
            break
        print(f"[{ts()}] {city}: widget viste forkert indhold ved knap {cand}, prøver næste")
        widget = ""
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

    if not widget:
        raise RuntimeError(f"Kunne ikke åbne widget for {city}")

    dump(f"{slug(city)}_widget.txt", widget)
    return widget


def classify(widget: str) -> tuple:
    """Returnér (status, detaljer). status: 'empty' | 'available'."""
    low = widget.lower()
    if EMPTY_PHRASE in low:
        return "empty", ""
    if "køb resale" in low:
        # Udtræk dato-linjer som detalje, fx "19 søndag - 13:00"
        lines = [l.strip() for l in widget.splitlines() if l.strip()]
        detail = " | ".join(lines[-8:])
        return "available", detail
    return "unknown", widget[-300:]


def main() -> None:
    state = load_state()
    findings = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="da-DK",
            viewport={"width": 1280, "height": 2000},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        dismiss_cookie_banner(page)

        present = cities_on_page(page)
        print(f"[{ts()}] Byer på siden: {present}")
        dump("fullpage.txt", page.inner_text("body"))

        for city in WATCH_CITIES:
            if city not in present:
                print(f"[{ts()}] {city}: ikke længere på siden, springer over")
                continue
            try:
                widget = open_city_widget(context, page, city, present)
            except Exception:
                print(f"[{ts()}] {city}: FEJL ved tjek")
                traceback.print_exc()
                continue

            status, detail = classify(widget)
            digest = hashlib.sha256(widget.encode()).hexdigest()
            prev = state.get(city, {})
            print(f"[{ts()}] {city}: {status.upper()} {('- ' + detail) if detail else ''}")

            if status == "available" and (prev.get("status") != "available" or prev.get("hash") != digest):
                findings.append((city, detail))
            if status == "unknown":
                print(f"[{ts()}] {city}: ukendt widget-indhold, tjek debug-dump")

            state[city] = {"status": status, "hash": digest, "checked": ts()}

        browser.close()

    save_state(state)

    for city, detail in findings:
        notify(
            f"GRØN {city}: Resale-billetter til salg!",
            f"Der er billetter på Resale-markedspladsen for {city} lige nu. "
            f"{detail}\nKøb straks: {URL}",
        )
        print(f"[{ts()}] NOTIFIKATION SENDT: {city}")


if __name__ == "__main__":
    main()

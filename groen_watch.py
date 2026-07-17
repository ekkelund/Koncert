#!/usr/bin/env python3
"""
Grøn Koncert Resale Watcher (v2)
Overvåger Resale-markedspladsen for Odense, Næstved og Valby
og sender push-notifikation via ntfy.sh ved ændringer.

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

# Byerne står i fast rækkefølge på siden. Resale-knapperne matches på indeks.
ALL_CITIES = ["Tårnby", "Kolding", "Aarhus", "Aalborg", "Esbjerg", "Odense", "Næstved", "Valby"]
WATCH_CITIES = ["Odense", "Næstved", "Valby"]

# Tekster der indikerer at der IKKE er billetter (justér efter debug-dumps)
NO_TICKETS_PATTERNS = [
    r"ingen\s+billetter",
    r"ingen\s+resale",
    r"ikke\s+.*til\s+salg",
    r"udsolgt",
    r"no\s+tickets",
    r"der\s+er\s+i\s+\u00f8jeblikket\s+ingen",
]
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


def looks_empty(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in NO_TICKETS_PATTERNS)


def dump(name: str, content: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / name).write_text(content, encoding="utf-8")


def collect_text(page, context) -> str:
    """Saml tekst fra siden, alle iframes og evt. nyåbnede faner."""
    texts = []
    for p in context.pages:
        try:
            texts.append(f"=== PAGE {p.url} ===\n" + p.inner_text("body"))
        except Exception:
            pass
        for frame in p.frames:
            try:
                if frame != p.main_frame:
                    texts.append(f"=== FRAME {frame.url} ===\n" + frame.inner_text("body"))
            except Exception:
                pass
    return "\n\n".join(texts)


def check_city(context, page, city: str) -> str:
    """Klik på byens Resale-knap (matchet på indeks) og returnér al synlig tekst."""
    idx = ALL_CITIES.index(city)

    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    buttons = page.locator("text=/Køb Resale/i")
    count = buttons.count()
    print(f"[{ts()}] {city}: fandt {count} resale-knapper på siden (forventer {len(ALL_CITIES)})")
    if count == 0:
        raise RuntimeError("Ingen 'Køb Resale'-knapper fundet")

    # Hvis antallet matcher antal byer, brug indeks. Ellers fallback: nærmeste efter overskrift.
    if count >= len(ALL_CITIES):
        target = buttons.nth(idx)
    else:
        target = buttons.first

    pages_before = len(context.pages)
    target.scroll_into_view_if_needed(timeout=10000)
    target.click(timeout=10000)
    page.wait_for_timeout(7000)

    combined = collect_text(page, context)
    dump(f"{slug(city)}.txt", combined)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{slug(city)}.png"), full_page=False)
    except Exception:
        pass

    # Luk evt. ny fane og modal
    while len(context.pages) > pages_before:
        try:
            context.pages[-1].close()
        except Exception:
            break
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    return combined


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

        # Acceptér cookie-banner hvis det findes
        for sel in [
            "#onetrust-accept-btn-handler",
            "text=/accepter alle/i",
            "text=/tillad alle/i",
            "text=/accepter/i",
            "button:has-text('OK')",
        ]:
            try:
                page.locator(sel).first.click(timeout=2500)
                print(f"[{ts()}] Cookie-banner lukket via {sel}")
                page.wait_for_timeout(1000)
                break
            except Exception:
                continue

        # Fuldt dump af forsiden til kalibrering
        dump("fullpage.txt", page.inner_text("body"))
        try:
            page.screenshot(path=str(DEBUG_DIR / "fullpage.png"), full_page=True)
        except Exception:
            pass

        for city in WATCH_CITIES:
            try:
                text = check_city(context, page, city)
            except Exception:
                print(f"[{ts()}] {city}: FEJL ved tjek")
                traceback.print_exc()
                continue

            snippet = text[-6000:]
            digest = hashlib.sha256(snippet.encode()).hexdigest()
            prev = state.get(city, {})

            empty = looks_empty(snippet)
            status = "TOM" if empty else "MULIGE BILLETTER"
            print(f"[{ts()}] {city}: {status}")

            if not empty and prev.get("hash") != digest:
                findings.append(city)

            state[city] = {"hash": digest, "empty": empty, "checked": ts()}

        browser.close()

    save_state(state)

    if findings:
        cities_str = ", ".join(findings)
        notify(
            f"GRØN: Mulige resale-billetter — {cities_str}!",
            f"Der er sket en ændring på Resale-markedspladsen for {cities_str}. "
            f"Skynd dig ind på {URL}",
        )
        print(f"[{ts()}] NOTIFIKATION SENDT: {cities_str}")


if __name__ == "__main__":
    main()

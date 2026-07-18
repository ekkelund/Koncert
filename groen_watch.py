#!/usr/bin/env python3
"""
Grøn Koncert Resale Watcher (v7.1)
Overvåger Resale-markedspladsen for Aalborg (test), Odense, Næstved og Valby
og sender push-notifikation via ntfy.sh, når der kommer billetter til salg.

Detektion (to niveauer, da "Køb Resale"-knappen altid vises i widgetten):
  1. Widget viser "Ingen resalebilletter tilgængelig pt." -> TOM
  2. Ellers klikkes "Køb Resale" og selve billetlisten aflæses:
     - "Ingen resalebilletter..." -> TOM
     - Priser/antal (kr/DKK/stk)  -> BILLETTER TIL SALG -> push STRAKS
     - Ukendt indhold             -> forsigtig push STRAKS + dump

Push sendes i samme sekund en by viser billetter, før de øvrige byer tjekkes.
Byer der er afholdt forsvinder fra siden og springes automatisk over.

Kører via GitHub Actions. PASSES gennemløb pr. kørsel med SLEEP_BETWEEN
sekunders pause imellem (styres via env i workflow-filen).
"""

import hashlib
import json
import os
import re
import time
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

PASSES = int(os.environ.get("PASSES", "2"))            # gennemløb pr. kørsel
SLEEP_BETWEEN = int(os.environ.get("SLEEP_BETWEEN", "240"))  # sekunder mellem gennemløb

ALL_CITIES = ["Tårnby", "Kolding", "Aarhus", "Aalborg", "Esbjerg", "Odense", "Næstved", "Valby"]
WATCH_CITIES = ["Aalborg", "Odense", "Næstved", "Valby"]

EMPTY_PHRASE = "ingen resalebillet"   # matcher "Ingen resalebilletter tilgængelig pt."
PRICE_PATTERN = re.compile(r"\d[\d.,]*\s*(?:kr|dkk)|\bstk\b|\bantal\b", re.I)
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


def find_widget_frame(context, city: str):
    """Find frame-objektet for Billetten-widgetten, der viser den givne by."""
    best = None
    best_len = 0
    for p in context.pages:
        for frame in p.frames:
            try:
                t = frame.inner_text("body")
            except Exception:
                continue
            if "GRØN -" in t.upper() and city.upper() in t.upper() and len(t) > best_len:
                best = frame
                best_len = len(t)
    return best


def open_city_widget(context, page, city: str, present: list):
    """Åbn byens resale-widget. Returnér frame-objektet (verificeret på bynavn)."""
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

    candidates = [idx * per_city + o for o in range(per_city)]
    for cand in candidates:
        if cand >= count:
            continue
        target = buttons.nth(cand)
        try:
            target.scroll_into_view_if_needed(timeout=8000)
            target.click(timeout=8000)
        except Exception as e:
            print(f"[{ts()}] {city}: klik på knap {cand} fejlede ({e})")
            continue
        page.wait_for_timeout(6000)

        frame = find_widget_frame(context, city)
        if frame is not None:
            return frame
        print(f"[{ts()}] {city}: widget viste forkert indhold ved knap {cand}, prøver næste")
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

    raise RuntimeError(f"Kunne ikke åbne widget for {city}")


def deep_check(frame, city: str) -> str:
    """Klik 'Køb Resale' inde i widgetten og returnér billetlistens tekst."""
    txt = frame.inner_text("body")
    dump(f"{slug(city)}_widget.txt", txt)

    if EMPTY_PHRASE in txt.lower():
        return txt  # allerede afgjort: tom

    # Klik på Køb Resale inde i widgetten for at se selve listen
    try:
        frame.locator("text=/Køb Resale/i").first.click(timeout=6000)
    except Exception as e:
        print(f"[{ts()}] {city}: kunne ikke klikke 'Køb Resale' i widget ({e})")
        return txt

    frame.page.wait_for_timeout(5000)

    # Frame kan have navigeret; find den igen og læs
    try:
        deep = frame.inner_text("body")
    except Exception:
        deep = ""
    if not deep:
        f2 = find_widget_frame(frame.page.context, city)
        deep = f2.inner_text("body") if f2 else txt

    dump(f"{slug(city)}_liste.txt", deep)
    return deep


def classify(text: str) -> tuple:
    """Returnér (status, detaljer). status: 'empty' | 'available' | 'unknown'."""
    low = text.lower()
    if EMPTY_PHRASE in low:
        return "empty", ""
    if PRICE_PATTERN.search(low):
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        detail = " | ".join(lines[-10:])
        return "available", detail
    return "unknown", text[-300:]


def run_pass(state: dict) -> None:
    """Ét fuldt gennemløb af alle overvågede byer. Push sendes STRAKS pr. by."""
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
                frame = open_city_widget(context, page, city, present)
                text = deep_check(frame, city)
            except Exception:
                print(f"[{ts()}] {city}: FEJL ved tjek")
                traceback.print_exc()
                continue

            status, detail = classify(text)
            digest = hashlib.sha256(text.encode()).hexdigest()
            prev = state.get(city, {})
            print(f"[{ts()}] {city}: {status.upper()} {('- ' + detail[:200]) if detail else ''}")

            changed = prev.get("status") != status or prev.get("hash") != digest

            # PUSH STRAKS - før næste by tjekkes
            if status == "available" and changed:
                notify(
                    f"GRØN {city}: Resale-billetter til salg!",
                    f"{detail}\nKøb straks: {URL}",
                    priority="urgent",
                )
                print(f"[{ts()}] NOTIFIKATION SENDT (urgent): {city}")
            elif status == "unknown" and changed:
                notify(
                    f"GRØN {city}: Muligvis billetter (ukendt format), tjek selv",
                    f"{detail}\nSe: {URL}",
                    priority="high",
                )
                print(f"[{ts()}] NOTIFIKATION SENDT (high): {city}")

            state[city] = {"status": status, "hash": digest, "checked": ts()}
            save_state(state)

        browser.close()


def main() -> None:
    state = load_state()

    for i in range(PASSES):
        if i > 0:
            print(f"[{ts()}] Venter {SLEEP_BETWEEN}s før gennemløb {i + 1}/{PASSES}")
            time.sleep(SLEEP_BETWEEN)
        print(f"[{ts()}] ── Gennemløb {i + 1}/{PASSES} ──")
        run_pass(state)
        save_state(state)


if __name__ == "__main__":
    main()

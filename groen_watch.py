#!/usr/bin/env python3
"""
Grøn Koncert Resale Watcher (v11) - selvstyrende kontinuerlig drift
Overvåger Resale-markedspladsen for Odense (lydløs), Næstved og Valby (urgent).

Scriptet styrer selv sin køretid: kører gennemløb ryg mod ryg i RUN_MINUTES
(standard 45) og stopper derefter pænt, så workflow-persist altid når at køre,
uanset yml-timeout. Ingen env-konfiguration nødvendig.

Detektion i det nye widget-format:
  Kalendervisning:
    - "Køb Resale"-knap ved datoen  -> billetter findes -> deep-check bekræfter
    - Kun "Udsolgt", ingen knap     -> TOM
  Billetliste (efter klik):
    - "N tilgængelige" (N>0) / pris (x xxx,xx kr) -> BILLETTER -> push STRAKS
    - "0 tilgængelige" / "ingen ..."              -> TOM
"""

import hashlib
import json
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ── Konfiguration ────────────────────────────────────────
URL = "https://groenkoncert.dk/billetter/"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "groen-ekkelund-billet-7391")
STATE_FILE = Path(__file__).parent / "groen_state.json"
DEBUG_DIR = Path(__file__).parent / "debug"

# Selvstyrende køretid: ignorerer gamle PASSES/SLEEP_BETWEEN-env bevidst.
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "45"))

ALL_CITIES = ["Tårnby", "Kolding", "Aarhus", "Aalborg", "Esbjerg", "Odense", "Næstved", "Valby"]
WATCH_CITIES = ["Odense", "Næstved", "Valby"]
CITY_DEADLINES_UTC = {}

# Push-prioritet pr. by. urgent = sirene, default = stille notifikation.
CITY_PRIORITY = {"Odense": "default"}   # øvrige byer = urgent

AVAIL_PATTERN = re.compile(r"\b([1-9]\d*)\s+tilgængelig", re.I)
ZERO_PATTERN = re.compile(r"\b0\s+tilgængelig|ingen\s+resalebillet|ingen\s+billetter\s+fundet|ingen\s+resultater", re.I)
PRICE_PATTERN = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}\s*kr", re.I)
WIDGET_BUTTON = re.compile(r"Find Resale|Køb Resale", re.I)
CALENDAR_MARKERS = re.compile(r"kalender", re.I)
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
                finder().click(timeout=2000)
                page.wait_for_timeout(1000)
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
    best = None
    best_len = 0
    for p in context.pages:
        for frame in p.frames:
            try:
                t = frame.inner_text("body")
            except Exception:
                continue
            if "GRØN" in t.upper() and city.upper() in t.upper() and len(t) > best_len:
                best = frame
                best_len = len(t)
    return best


def open_city_widget(context, page, city: str, present: list):
    idx = present.index(city)

    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
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
            target.scroll_into_view_if_needed(timeout=6000)
            target.click(timeout=6000)
        except Exception as e:
            print(f"[{ts()}] {city}: klik på knap {cand} fejlede ({e})")
            continue
        page.wait_for_timeout(4000)

        frame = find_widget_frame(context, city)
        if frame is not None:
            return frame
        print(f"[{ts()}] {city}: widget viste forkert indhold ved knap {cand}, prøver næste")
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

    raise RuntimeError(f"Kunne ikke åbne widget for {city}")


def deep_check(frame, city: str) -> str:
    """Aflæs widgetten. Kalender uden 'Køb Resale'-knap = tom (stop der).
    Ellers klik ind til billetlisten og returnér dens tekst."""
    txt = frame.inner_text("body")
    dump(f"{slug(city)}_widget.txt", txt)

    if AVAIL_PATTERN.search(txt) or ZERO_PATTERN.search(txt):
        return txt

    if CALENDAR_MARKERS.search(txt) and not WIDGET_BUTTON.search(txt):
        return txt

    for level in range(2):
        try:
            btn = frame.locator(f"text=/{WIDGET_BUTTON.pattern}/i").first
            btn.click(timeout=5000)
        except Exception as e:
            print(f"[{ts()}] {city}: intet klik på niveau {level + 1} ({e})")
            break
        frame.page.wait_for_timeout(3500)
        try:
            txt = frame.inner_text("body")
        except Exception:
            f2 = find_widget_frame(frame.page.context, city)
            txt = f2.inner_text("body") if f2 else txt
        dump(f"{slug(city)}_liste_{level + 1}.txt", txt)
        if AVAIL_PATTERN.search(txt) or ZERO_PATTERN.search(txt) or PRICE_PATTERN.search(txt):
            break

    return txt


def classify(text: str) -> tuple:
    """Returnér (status, detaljer). status: 'empty' | 'available' | 'unknown'."""
    avail = AVAIL_PATTERN.search(text)
    price = PRICE_PATTERN.search(text)

    if avail:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        keep = [l for l in lines if re.search(r"tilgængelig|billet|kr\.|jul\.|normalpris", l, re.I)]
        detail = " | ".join(keep[:12]) if keep else " | ".join(lines[-10:])
        return "available", f"{avail.group(1)} tilgængelige - {detail}"
    if price:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        keep = [l for l in lines if re.search(r"kr|billet|jul", l, re.I)]
        return "available", " | ".join(keep[:12])
    if ZERO_PATTERN.search(text):
        return "empty", ""
    if CALENDAR_MARKERS.search(text) and not WIDGET_BUTTON.search(text):
        return "empty", ""
    return "unknown", text[-300:]


def run_pass(state: dict, browser) -> None:
    context = browser.new_context(
        locale="da-DK",
        viewport={"width": 1280, "height": 2000},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
    )
    try:
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        dismiss_cookie_banner(page)

        present = cities_on_page(page)
        print(f"[{ts()}] Byer på siden: {present}")
        dump("fullpage.txt", page.inner_text("body"))

        for city in WATCH_CITIES:
            deadline = CITY_DEADLINES_UTC.get(city)
            if deadline and datetime.now(timezone.utc) >= deadline:
                print(f"[{ts()}] {city}: udløbet, springer over")
                continue
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
            base_prio = CITY_PRIORITY.get(city, "urgent")

            if status == "available" and changed:
                notify(
                    f"GRØN {city}: Resale-billetter til salg!",
                    f"{detail}\nKøb straks: {URL}",
                    priority=base_prio,
                )
                print(f"[{ts()}] NOTIFIKATION SENDT ({base_prio}): {city}")
            elif status == "unknown" and changed:
                unknown_prio = "default" if base_prio == "default" else "high"
                notify(
                    f"GRØN {city}: Muligvis billetter (ukendt format), tjek selv",
                    f"{detail}\nSe: {URL}",
                    priority=unknown_prio,
                )
                print(f"[{ts()}] NOTIFIKATION SENDT ({unknown_prio}): {city}")

            state[city] = {"status": status, "hash": digest, "checked": ts()}
            save_state(state)
    finally:
        context.close()


def main() -> None:
    state = load_state()
    start = time.monotonic()
    deadline_s = RUN_MINUTES * 60
    i = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        while time.monotonic() - start < deadline_s:
            i += 1
            elapsed = (time.monotonic() - start) / 60
            print(f"[{ts()}] ── Gennemløb {i} (elapsed {elapsed:.1f}/{RUN_MINUTES:.0f} min) ──")
            try:
                run_pass(state, browser)
            except Exception:
                print(f"[{ts()}] Gennemløb {i} fejlede, fortsætter")
                traceback.print_exc()
            save_state(state)
        browser.close()

    print(f"[{ts()}] Køretid opbrugt efter {i} gennemløb, stopper pænt")


if __name__ == "__main__":
    main()

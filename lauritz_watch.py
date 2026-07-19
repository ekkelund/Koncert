#!/usr/bin/env python3
"""
Lauritz Design Watcher (v1)
Finder varer på Lauritz.com der:
  1. Udløber inden for de næste 12 timer
  2. Matcher en kendt designer (whitelist nedenfor)
  3. Har aktuelt bud / næste bud UNDER 50% af vurderingen

Sender ntfy-push med links til fundne varer. Kører via GitHub Actions.

Første kørsler er kalibreringskørsler: sidens struktur dumpes til debug/,
inkl. opsnappede JSON-API-svar, så selektorer og felter kan finjusteres.
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
BASE = "https://www.lauritz.com"
LIST_URL = f"{BASE}/da/auctions"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "lauritz-ekkelund-design-7391")
STATE_FILE = Path(__file__).parent / "lauritz_state.json"
DEBUG_DIR = Path(__file__).parent / "debug_lauritz"

HOURS_WINDOW = float(os.environ.get("HOURS_WINDOW", "12"))
MAX_RATIO = float(os.environ.get("MAX_RATIO", "0.5"))   # bud/vurdering
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))       # a 60 varer pr. side

# Kendte designere (matches case-insensitivt i titel/beskrivelse)
DESIGNERS = [
    "Hans J. Wegner", "Wegner", "Finn Juhl", "Arne Jacobsen", "Poul Kjærholm",
    "Kjærholm", "Børge Mogensen", "Mogensen", "Verner Panton", "Panton",
    "Poul Henningsen", "PH-lampe", "PH 5", "PH5", "Nanna Ditzel", "Kaare Klint",
    "Ole Wanscher", "Grete Jalk", "Hans Olsen", "Arne Vodder", "Illum Wikkelsø",
    "Kai Kristiansen", "Peter Hvidt", "Mogens Koch", "Alvar Aalto", "Aalto",
    "Bruno Mathsson", "Charles Eames", "Eames", "Le Corbusier", "Mies van der Rohe",
    "Eero Saarinen", "Saarinen", "Arne Norell", "Piet Hein", "Jørn Utzon",
    "Vico Magistretti", "Achille Castiglioni", "Gubi", "Louis Poulsen",
    "Fritz Hansen", "Carl Hansen", "Fredericia", "Getama", "France & Søn",
    "Bang & Olufsen", "Georg Jensen", "Royal Copenhagen", "Bjørn Wiinblad",
    "Axel Salto", "Arne Bang", "Kähler", "Saxbo", "Per Lütken", "Holmegaard",
]
# ─────────────────────────────────────────────────────────


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def notify(title: str, message: str, priority: str = "high") -> None:
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": "hammer,moneybag",
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


def parse_dkk(s: str):
    """'12.500' / '12.500 kr' -> 12500.0"""
    m = re.search(r"[\d.]+", s.replace("\u00a0", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", ""))
    except ValueError:
        return None


def parse_time_left(s: str):
    """Parse resttid som 'Xd Yt', 'X timer', 'XX:YY:ZZ', 'X min' -> timer (float)."""
    low = s.lower().replace("\u00a0", " ")
    # dage
    d = re.search(r"(\d+)\s*d", low)
    h = re.search(r"(\d+)\s*(?:t\b|tim)", low)
    m = re.search(r"(\d+)\s*min", low)
    hhmmss = re.search(r"(\d+):(\d{2}):(\d{2})", low)
    if hhmmss:
        return int(hhmmss.group(1)) + int(hhmmss.group(2)) / 60
    hours = 0.0
    found = False
    if d:
        hours += int(d.group(1)) * 24
        found = True
    if h:
        hours += int(h.group(1))
        found = True
    if m:
        hours += int(m.group(1)) / 60
        found = True
    return hours if found else None


def match_designer(text: str):
    low = text.lower()
    for name in DESIGNERS:
        if name.lower() in low:
            return name
    return None


def dismiss_cookies(page) -> None:
    for finder in [
        lambda: page.get_by_role("button", name=re.compile("afvis", re.I)).first,
        lambda: page.get_by_role("button", name=re.compile("kun nødvendige", re.I)).first,
        lambda: page.get_by_role("button", name=re.compile("accepter", re.I)).first,
        lambda: page.locator("#onetrust-accept-btn-handler").first,
        lambda: page.locator("#CybotCookiebotDialogBodyButtonDecline").first,
        lambda: page.locator("#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll").first,
    ]:
        try:
            finder().click(timeout=2500)
            page.wait_for_timeout(1200)
            print(f"[{ts()}] Cookie-banner håndteret")
            return
        except Exception:
            continue


def main() -> None:
    state = load_state()
    notified = state.get("notified", {})
    api_dumps = []
    findings = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="da-DK",
            viewport={"width": 1400, "height": 2400},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Opsnap JSON-API-svar til kalibrering (afslører ofte et rent API)
        def on_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                if "json" in ct and any(k in resp.url.lower() for k in ["search", "auction", "item", "lot", "api"]):
                    body = resp.text()
                    if len(body) > 200 and len(api_dumps) < 8:
                        api_dumps.append(f"=== {resp.url} ===\n{body[:20000]}")
            except Exception:
                pass

        page.on("response", on_response)

        # Sortér efter kortest resttid hvis muligt via URL-parametre; ellers standard
        page.goto(LIST_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        dismiss_cookies(page)
        page.wait_for_timeout(3000)

        dump("listpage.txt", page.inner_text("body"))
        dump("listpage.html", page.content()[:300000])
        if api_dumps:
            dump("api_responses.txt", "\n\n".join(api_dumps))
            print(f"[{ts()}] Opsnappede {len(api_dumps)} JSON-svar (se debug_lauritz/api_responses.txt)")

        # Find varekort: links til varesider
        cards = page.locator("a[href*='/da/']").all()
        seen_links = set()
        candidates = 0

        for a in cards:
            try:
                href = a.get_attribute("href") or ""
                text = a.inner_text().strip()
            except Exception:
                continue
            # Varesider har typisk et numerisk id i URL'en
            if not re.search(r"/\d{5,}", href):
                continue
            if href in seen_links or not text:
                continue
            seen_links.add(href)

            vurdering_m = re.search(r"vurdering\s*\n?\s*([\d.\u00a0]+)", text, re.I)
            bud_m = re.search(r"(?:næste bud|bud)\s*\n?\s*([\d.\u00a0]+)", text, re.I)
            if not vurdering_m or not bud_m:
                continue
            vurdering = parse_dkk(vurdering_m.group(1))
            bud = parse_dkk(bud_m.group(1))
            if not vurdering or not bud or vurdering <= 0:
                continue

            hours_left = parse_time_left(text)
            designer = match_designer(text)
            ratio = bud / vurdering
            candidates += 1

            ok_time = hours_left is not None and hours_left <= HOURS_WINDOW
            if designer and ratio < MAX_RATIO and ok_time:
                item_id = hashlib.sha256(href.encode()).hexdigest()[:16]
                if notified.get(item_id):
                    continue
                url = href if href.startswith("http") else BASE + href
                title_line = text.splitlines()[0][:80]
                findings.append(
                    f"{designer}: {title_line}\n"
                    f"Bud {int(bud)} kr / vurdering {int(vurdering)} kr ({ratio:.0%}) "
                    f"- slutter om ~{hours_left:.1f} t\n{url}"
                )
                notified[item_id] = ts()

        print(f"[{ts()}] Gennemgik {len(seen_links)} links, {candidates} med pris-data, {len(findings)} match")

        browser.close()

    state["notified"] = notified
    save_state(state)

    if findings:
        body = "\n\n".join(findings[:6])
        notify(
            f"LAURITZ: {len(findings)} designerfund under 50% af vurdering",
            body,
        )
        print(f"[{ts()}] NOTIFIKATION SENDT: {len(findings)} varer")
    elif candidates == 0:
        print(f"[{ts()}] ADVARSEL: ingen varer med pris-data fundet - tjek debug_lauritz/ for kalibrering")


if __name__ == "__main__":
    main()

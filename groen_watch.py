#!/usr/bin/env python3
"""
Grøn Koncert Resale Watcher
Overvåger Resale-markedspladsen for Odense, Næstved og Valby
og sender push-notifikation via ntfy.sh ved ændringer.

Kører automatisk via GitHub Actions (se .github/workflows/watch.yml).
"""

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Konfiguration ────────────────────────────────────────
CITIES = ["Odense", "Næstved", "Valby"]
URL = "https://groenkoncert.dk/billetter/"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "groen-ekkelund-billet-7391")
STATE_FILE = Path(__file__).parent / "groen_state.json"
DEBUG_DIR = Path(__file__).parent / "debug"

# Tekster der indikerer at der IKKE er billetter (justér efter første kørsel
# ved at kigge i debug-artifacts under Actions)
NO_TICKETS_PATTERNS = [
    r"ingen\s+billetter",
    r"ingen\s+resale",
    r"ikke\s+.*til\s+salg",
    r"udsolgt",
    r"no\s+tickets",
]
# ─────────────────────────────────────────────────────────


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


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def looks_empty(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in NO_TICKETS_PATTERNS)


def check_city(page, city: str) -> str:
    """Klik på byens Resale-knap og returnér den tekst, der vises."""
    heading = page.locator(f"text=/^{city}$/i").first
    heading.scroll_into_view_if_needed(timeout=10000)

    section = heading.locator(
        "xpath=ancestor::*[self::section or self::div][.//text()[contains(., 'Resale')]][1]"
    )
    button = section.locator("text=/Køb Resale/i").first
    button.click(timeout=10000)

    page.wait_for_timeout(6000)

    texts = [page.inner_text("body")]
    for frame in page.frames:
        try:
            if frame != page.main_frame:
                texts.append(frame.inner_text("body"))
        except Exception:
            pass
    combined = "\n---FRAME---\n".join(texts)

    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / f"{city.lower().replace('æ', 'ae')}.txt").write_text(
        combined, encoding="utf-8"
    )

    page.keyboard.press("Escape")
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    return combined


def main() -> None:
    state = load_state()
    findings = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="da-DK",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # Acceptér cookie-banner hvis det findes
        for sel in [
            "text=/accepter/i",
            "text=/tillad alle/i",
            "#onetrust-accept-btn-handler",
        ]:
            try:
                page.locator(sel).first.click(timeout=3000)
                break
            except Exception:
                continue

        for city in CITIES:
            try:
                text = check_city(page, city)
            except (PWTimeout, Exception) as e:
                print(f"[{ts()}] {city}: fejl ved tjek ({e})")
                continue

            snippet = text[-4000:]
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

#!/usr/bin/env python3
"""
Ticketmaster Resale Watcher
===========================
Overvaager primaer- OG resale-status for udvalgte Ticketmaster-events
og sender push-notifikation via ntfy.sh naar status flipper til
"billetter tilgaengelige".

Arkitektur (samme princip som groen_watch.py, men uden Playwright):
  1) Discovery API  -> find universal event-IDs ud fra artist + lande
  2) Inventory Status API -> hent status + resaleStatus pr. event
  3) Sammenlign mod state-fil -> push kun ved aendring TIL "available"

Modes:
  discover  Slaa events op og print dem. Ingen overvaagning, ingen push.
  diag      Ét gennemloeb med fuldt raat JSON-dump til debug/. Ingen push.
  watch     Overvaagningsloop (default). Bruges af GitHub Actions.

Kraever env:
  TM_API_KEY   Consumer Key fra developer.ticketmaster.com
  NTFY_TOPIC   (optional) default nedenfor
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- Konfiguration ------------------------------------------------
API_KEY = os.environ.get("TM_API_KEY", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "tm-ekkelund-billet-4412").strip()

DISCOVERY_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
INVENTORY_URL = "https://app.ticketmaster.com/inventory-status/v1/availability"

# Hvilken artist og hvilke lande vi leder efter
KEYWORD = os.environ.get("TM_KEYWORD", "The Neighbourhood")
COUNTRIES = [c.strip() for c in os.environ.get("TM_COUNTRIES", "DE,FR,ES").split(",") if c.strip()]

# Prioritet pr. by. Hoej prioritet = paatraengende push (ntfy priority 5).
# Byer der ikke staar her overvaages stadig, men med normal prioritet.
CITY_PRIORITY = {
    "Cologne": 5,
    "Koeln": 5,
    "Koln": 5,
    "Hamburg": 4,
    "Paris": 3,
    "Madrid": 3,
}

# Vaerdier fra Inventory Status API der betyder "der er noget at koebe"
AVAILABLE_STATES = {"TICKETS_AVAILABLE", "FEW_TICKETS_LEFT", "LIMITED_AVAILABILITY"}

STATE_FILE = Path(__file__).parent / "tm_state.json"
DEBUG_DIR = Path(__file__).parent / "debug"

# Loop-parametre. Holdes bevidst under GitHub Actions' 75-min timeout,
# saa state-filen altid faar lov at blive committet (laeren fra Groen v11).
LOOP_MINUTES = int(os.environ.get("TM_LOOP_MINUTES", "50"))
PASS_INTERVAL = int(os.environ.get("TM_PASS_INTERVAL", "60"))       # sek mellem inventory-kald
DISCOVERY_EVERY = int(os.environ.get("TM_DISCOVERY_EVERY", "20"))    # pass mellem discovery-kald

TIMEOUT = 25


# --- Smaa hjaelpere -----------------------------------------------
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def die(msg, code=1):
    log(f"FEJL: {msg}")
    sys.exit(code)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"State-fil ulaeselig ({e}), starter forfra")
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def push(title, message, priority=4, tags="ticket", click=None):
    """Send ntfy-notifikation. Fejler stille - en tabt push maa ikke vaelte loopet."""
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": str(priority),
        "Tags": tags,
    }
    if click:
        headers["Click"] = click
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        log(f"  push -> {r.status_code}: {title}")
    except Exception as e:
        log(f"  push fejlede: {e}")


# --- Lag 1: Discovery ---------------------------------------------
def discover_events():
    """Returner liste af dicts: id, name, date, city, country, url, onsale_status."""
    found = {}
    for cc in COUNTRIES:
        params = {
            "apikey": API_KEY,
            "keyword": KEYWORD,
            "countryCode": cc,
            "size": 50,
            "sort": "date,asc",
        }
        try:
            r = requests.get(DISCOVERY_URL, params=params, timeout=TIMEOUT)
        except Exception as e:
            log(f"  discovery {cc} fejlede: {e}")
            continue

        if r.status_code != 200:
            log(f"  discovery {cc} -> HTTP {r.status_code}: {r.text[:300]}")
            continue

        events = r.json().get("_embedded", {}).get("events", [])
        log(f"  discovery {cc} -> {len(events)} events")

        for ev in events:
            venues = ev.get("_embedded", {}).get("venues", [{}])
            venue = venues[0] if venues else {}
            city = (venue.get("city") or {}).get("name", "?")
            found[ev["id"]] = {
                "id": ev["id"],
                "name": ev.get("name", "?"),
                "date": (ev.get("dates", {}).get("start", {}) or {}).get("localDate", "?"),
                "time": (ev.get("dates", {}).get("start", {}) or {}).get("localTime", ""),
                "city": city,
                "venue": venue.get("name", "?"),
                "country": cc,
                "url": ev.get("url", ""),
                "onsale_status": (ev.get("dates", {}).get("status", {}) or {}).get("code", "?"),
            }
    return list(found.values())


# --- Lag 2: Inventory Status --------------------------------------
def fetch_inventory(event_ids):
    """Ét batch-kald for op til 50 event-IDs. Returner {eventId: rawdict}."""
    out = {}
    for i in range(0, len(event_ids), 50):
        chunk = event_ids[i : i + 50]
        params = {"apikey": API_KEY, "events": ",".join(chunk)}
        try:
            r = requests.get(INVENTORY_URL, params=params, timeout=TIMEOUT)
        except Exception as e:
            log(f"  inventory fejlede: {e}")
            continue

        if r.status_code != 200:
            log(f"  inventory -> HTTP {r.status_code}: {r.text[:300]}")
            continue

        # Kvote-headers er vaerd at holde oeje med
        quota = r.headers.get("Ratelimit-Quota-Available")
        if quota:
            log(f"  kvote tilbage: {quota}")

        data = r.json()
        if isinstance(data, dict):
            data = [data]
        for row in data:
            eid = row.get("eventId")
            if eid:
                out[eid] = row
    return out


def priority_for(city):
    for key, prio in CITY_PRIORITY.items():
        if key.lower() in (city or "").lower():
            return prio
    return 3


# --- Kerne: ét gennemloeb ----------------------------------------
def run_pass(catalog, state, notify=True):
    """catalog: {eventId: metadata}. Muterer state. Returner antal notifikationer."""
    if not catalog:
        log("  intet katalog - springer pass over")
        return 0

    inv = fetch_inventory(list(catalog.keys()))
    if not inv:
        log("  ingen inventory-data i dette pass")
        return 0

    sent = 0
    for eid, row in inv.items():
        meta = catalog.get(eid, {})
        label = f"{meta.get('city', '?')} {meta.get('date', '')}".strip()

        primary = (row.get("status") or "UNKNOWN").upper()
        resale = (row.get("resaleStatus") or "NONE").upper()
        prev = state.get(eid, {})
        prev_primary = (prev.get("status") or "").upper()
        prev_resale = (prev.get("resaleStatus") or "").upper()

        first_seen = eid not in state
        resale_opened = resale in AVAILABLE_STATES and prev_resale not in AVAILABLE_STATES
        primary_opened = primary in AVAILABLE_STATES and prev_primary not in AVAILABLE_STATES

        log(f"  {label:28s} primary={primary:24s} resale={resale}")

        if notify and not first_seen and (resale_opened or primary_opened):
            kind = "RESALE" if resale_opened else "ALMINDELIGE"
            title = f"{kind} billetter: {label}"
            body_lines = [
                meta.get("name", ""),
                f"{meta.get('venue', '')}, {meta.get('city', '')}",
                f"{meta.get('date', '')} {meta.get('time', '')}".strip(),
                "",
                f"Primaer: {primary}",
                f"Resale:  {resale}",
            ]
            price = row.get("priceRanges")
            if price:
                body_lines.append(f"Pris: {json.dumps(price, ensure_ascii=False)}")
            push(
                title,
                "\n".join(x for x in body_lines if x is not None),
                priority=priority_for(meta.get("city")),
                tags="tickets,rotating_light" if resale_opened else "tickets",
                click=meta.get("url") or None,
            )
            sent += 1

        state[eid] = {
            "name": meta.get("name"),
            "city": meta.get("city"),
            "date": meta.get("date"),
            "url": meta.get("url"),
            "status": primary,
            "resaleStatus": resale,
            "seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    save_state(state)
    return sent


# --- Modes --------------------------------------------------------
def mode_discover():
    events = discover_events()
    if not events:
        die("Ingen events fundet. Tjek TM_API_KEY, KEYWORD og COUNTRIES.")
    events.sort(key=lambda e: (e["date"], e["city"]))
    print(f"\n{len(events)} events fundet for '{KEYWORD}' i {','.join(COUNTRIES)}:\n")
    print(f"{'DATO':11s} {'BY':14s} {'ID':20s} {'ONSALE':10s} NAVN")
    print("-" * 100)
    for e in events:
        print(f"{e['date']:11s} {e['city']:14s} {e['id']:20s} {e['onsale_status']:10s} {e['name']}")
    print()


def mode_diag():
    DEBUG_DIR.mkdir(exist_ok=True)
    events = discover_events()
    if not events:
        die("Ingen events fundet.")
    catalog = {e["id"]: e for e in events}

    (DEBUG_DIR / "discovery.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    inv = fetch_inventory(list(catalog.keys()))
    (DEBUG_DIR / "inventory.json").write_text(
        json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n=== RAA INVENTORY-RESPONS ===")
    print(json.dumps(inv, ensure_ascii=False, indent=2))
    print("\n=== FORTOLKET ===")
    for eid, row in inv.items():
        meta = catalog.get(eid, {})
        print(
            f"{meta.get('date','?'):11s} {meta.get('city','?'):14s} "
            f"status={row.get('status')} resaleStatus={row.get('resaleStatus')}"
        )
    missing = set(catalog) - set(inv)
    if missing:
        print(f"\nADVARSEL: {len(missing)} events fik intet inventory-svar:")
        for eid in missing:
            m = catalog[eid]
            print(f"  {m['date']} {m['city']} ({eid})")
    print(f"\nDump skrevet til {DEBUG_DIR}/")


def mode_watch():
    state = load_state()
    baseline = not state
    catalog = {}
    deadline = time.time() + LOOP_MINUTES * 60
    p = 0
    total_sent = 0

    log(f"Watch startet. Loop {LOOP_MINUTES} min, interval {PASS_INTERVAL}s, topic {NTFY_TOPIC}")
    if baseline:
        log("Ingen state-fil - foerste koersel bliver baseline (ingen alarmer)")

    while time.time() < deadline:
        p += 1
        log(f"--- pass {p} ---")

        if not catalog or (p - 1) % DISCOVERY_EVERY == 0:
            events = discover_events()
            if events:
                catalog = {e["id"]: e for e in events}
                log(f"  katalog: {len(catalog)} events")
            elif not catalog:
                log("  discovery tom og intet cached katalog - venter")

        sent = run_pass(catalog, state, notify=not baseline)
        total_sent += sent

        if baseline:
            lines = [
                f"{v['city']} {v['date']}: primaer={v['status']} resale={v['resaleStatus']}"
                for v in sorted(state.values(), key=lambda x: (x.get("date") or ""))
            ]
            push(
                "Watcher er i luften",
                "Baseline registreret:\n\n" + "\n".join(lines),
                priority=2,
                tags="white_check_mark",
            )
            baseline = False

        remaining = deadline - time.time()
        if remaining <= PASS_INTERVAL:
            break
        time.sleep(PASS_INTERVAL)

    log(f"Watch afsluttet efter {p} pass. {total_sent} notifikationer sendt.")


def main():
    if not API_KEY:
        die("TM_API_KEY mangler. Saet den som GitHub secret eller i miljoeet.")
    mode = (sys.argv[1] if len(sys.argv) > 1 else "watch").lower()
    if mode == "discover":
        mode_discover()
    elif mode == "diag":
        mode_diag()
    elif mode == "watch":
        mode_watch()
    else:
        die(f"Ukendt mode '{mode}'. Brug: discover | diag | watch")


if __name__ == "__main__":
    main()

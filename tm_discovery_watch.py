#!/usr/bin/env python3
"""
Ticketmaster Discovery Watcher
==============================
Overvaager The Neighbourhood-events via Ticketmasters *offentlige* Discovery API.

Baggrund: Inventory Status API (det endpoint der har det praecise felt
`resaleStatus`) er forbeholdt autoriserede klienter og svarer 401
"InvalidApiKeyForGivenResource" paa en almindelig developer-noegle.
Discovery API er derimod aabent for alle noegler.

Designidé: Vi ved ikke hvilket felt i Discovery-svaret der aendrer sig naar
billetter bliver tilgaengelige. Derfor overvaager scriptet IKKE en haandfuld
kendte felter - det gemmer hele event-objektet og rapporterer den praecise
diff ved enhver aendring. Saa opdager vi signalet i stedet for at gaette det.

Modes:
  diag    Ét gennemloeb. Dumper hele event-objektet pr. event. Ingen push.
  watch   Overvaagningsloop (default).

Kraever env:
  TM_API_KEY   Consumer Key fra developer.ticketmaster.com
  NTFY_TOPIC   (optional)
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

DISCOVERY_SEARCH = "https://app.ticketmaster.com/discovery/v2/events.json"
DISCOVERY_EVENT = "https://app.ticketmaster.com/discovery/v2/events/{id}.json"

KEYWORD = os.environ.get("TM_KEYWORD", "The Neighbourhood")
COUNTRIES = [c.strip() for c in os.environ.get("TM_COUNTRIES", "DE,ES").split(",") if c.strip()]

# Kendte maal. Overvaages altid, selv hvis keyword-soegningen skulle svigte.
TARGETS = {
    "Z698xZC2Z16vOZPMuk": ("Koeln", "hovedevent", 5),
    "Z698xZC2Z16v4dFgaZ": ("Koeln", "box-seat", 4),
    "Z698xZC2Z1keFAdv0": ("Hamburg", "hovedevent", 4),
    "Z698xZC2Z16vGwQZGI": ("Hamburg", "box-seat", 3),
    "Z698xZ2qZ1kf60z8x": ("Madrid", "hovedevent", 3),
}

# Felter der stoejer uden at betyde noget. Fjernes foer sammenligning.
NOISE_KEYS = {"_links", "images", "locale", "test", "seatmap", "products", "aliases"}

# Naar disse ord optraeder i en aendret felt-sti, er det interessant nok
# til hoej prioritet - ogsaa hvis feltet er nyt og udokumenteret.
HOT_WORDS = ("resale", "avail", "status", "price", "ticketlimit", "onsale", "offsale", "sales", "limit")

# Vaerdier der betyder at doeren lukkede. De skal ikke vaekke nogen kl. 3.
BAD_VALUES = ("offsale", "cancelled", "canceled", "postponed", "rescheduled", "false")

STATE_FILE = Path(__file__).parent / "tm_disc_state.json"
DEBUG_DIR = Path(__file__).parent / "debug"

LOOP_MINUTES = int(os.environ.get("TM_LOOP_MINUTES", "50"))
PASS_INTERVAL = int(os.environ.get("TM_PASS_INTERVAL", "90"))
TIMEOUT = 25
THROTTLE = 0.6   # Public API-kvote er 2 kald/sek. Vi holder os paent under.


# --- Hjaelpere ----------------------------------------------------
def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg):
    log(f"FEJL: {msg}")
    sys.exit(1)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"State ulaeselig ({e}) - starter forfra")
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def push(title, message, priority=4, tags="ticket", click=None):
    headers = {"Title": title.encode("utf-8"), "Priority": str(priority), "Tags": tags}
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


def strip_noise(obj, _depth=0):
    """Fjern felter der aendrer sig uden at betyde noget."""
    if _depth > 12:
        return obj
    if isinstance(obj, dict):
        return {
            k: strip_noise(v, _depth + 1)
            for k, v in obj.items()
            if k not in NOISE_KEYS
        }
    if isinstance(obj, list):
        return [strip_noise(v, _depth + 1) for v in obj]
    return obj


def flatten(obj, prefix=""):
    """Fold et nestet objekt ud til {sti: vaerdi} saa vi kan diffe praecist."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def diff_flat(old, new):
    """Returner liste af (sti, foer, efter). None markerer at feltet manglede."""
    changes = []
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k, "\x00MISSING"), new.get(k, "\x00MISSING")
        if a != b:
            changes.append((k, None if a == "\x00MISSING" else a, None if b == "\x00MISSING" else b))
    return changes


def is_hot(path):
    p = path.lower()
    return any(w in p for w in HOT_WORDS)


def is_bad_turn(changes):
    """True hvis alle interessante aendringer peger den forkerte vej.
    Saa er det information, ikke en alarm."""
    hot = [c for c in changes if is_hot(c[0])]
    if not hot:
        return False
    return all(str(b).strip().lower() in BAD_VALUES for _, _, b in hot)


def fmt(v):
    if v is None:
        return "(manglede)"
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= 70 else s[:67] + "..."


# --- Hentning -----------------------------------------------------
def fetch_search():
    """Keyword-soegning pr. land. Fanger ogsaa events vi ikke kender endnu."""
    found = {}
    for cc in COUNTRIES:
        params = {
            "apikey": API_KEY, "keyword": KEYWORD, "countryCode": cc,
            "size": 50, "sort": "date,asc",
        }
        try:
            r = requests.get(DISCOVERY_SEARCH, params=params, timeout=TIMEOUT)
        except Exception as e:
            log(f"  search {cc} fejlede: {e}")
            continue
        if r.status_code != 200:
            log(f"  search {cc} -> HTTP {r.status_code}: {r.text[:200]}")
            continue
        for ev in r.json().get("_embedded", {}).get("events", []):
            found[ev["id"]] = ev
        time.sleep(THROTTLE)
    return found


def fetch_event(eid):
    """Event-detail. Kan indeholde felter som soegeresultatet udelader."""
    try:
        r = requests.get(
            DISCOVERY_EVENT.format(id=eid), params={"apikey": API_KEY}, timeout=TIMEOUT
        )
    except Exception as e:
        log(f"  event {eid} fejlede: {e}")
        return None
    if r.status_code != 200:
        log(f"  event {eid} -> HTTP {r.status_code}")
        return None
    return r.json()


def collect():
    """Samlet billede: soegning + detailopslag paa alle kendte maal."""
    events = fetch_search()
    for eid in TARGETS:
        if eid not in events:
            ev = fetch_event(eid)
            if ev:
                events[eid] = ev
            time.sleep(THROTTLE)
    return events


def describe(ev):
    v = (ev.get("_embedded", {}).get("venues") or [{}])[0]
    return {
        "name": ev.get("name", "?"),
        "date": (ev.get("dates", {}).get("start", {}) or {}).get("localDate", "?"),
        "city": (v.get("city") or {}).get("name", "?"),
        "venue": v.get("name", "?"),
        "url": ev.get("url", ""),
        "onsale": (ev.get("dates", {}).get("status", {}) or {}).get("code", "?"),
    }


def price_summary(ev):
    prs = ev.get("priceRanges") or []
    if not prs:
        return "ingen prisdata"
    return " | ".join(
        f"{p.get('type','?')} {p.get('min','?')}-{p.get('max','?')} {p.get('currency','')}".strip()
        for p in prs
    )


# --- Kerne --------------------------------------------------------
def run_pass(state, notify=True):
    events = collect()
    if not events:
        log("  intet svar - springer pass over")
        return 0

    sent = 0
    for eid, ev in events.items():
        meta = describe(ev)
        city, variant, prio = TARGETS.get(eid, (meta["city"], "ukendt", 2))
        label = f"{city} {meta['date']}" + (f" ({variant})" if variant != "hovedevent" else "")

        flat = flatten(strip_noise(ev))
        prev = state.get(eid)

        if prev is None:
            log(f"  NY  {label:34s} onsale={meta['onsale']:9s} {price_summary(ev)}")
            if notify:
                push(
                    f"Nyt event: {label}",
                    f"{meta['name']}\n{meta['venue']}, {meta['city']}\n{meta['date']}\n\n"
                    f"Status: {meta['onsale']}\nPris: {price_summary(ev)}",
                    priority=prio, tags="new", click=meta["url"] or None,
                )
                sent += 1
        else:
            changes = diff_flat(prev.get("flat", {}), flat)
            if changes:
                hot = [c for c in changes if is_hot(c[0])]
                log(f"  AEND {label:34s} {len(changes)} felt(er), {len(hot)} interessante")
                for path, a, b in changes[:25]:
                    mark = "*" if is_hot(path) else " "
                    log(f"       {mark} {path}: {fmt(a)} -> {fmt(b)}")

                if notify:
                    shown = (hot or changes)[:12]
                    body = "\n".join(f"{p}\n  {fmt(a)} -> {fmt(b)}" for p, a, b in shown)
                    extra = len(hot or changes) - len(shown)
                    if extra > 0:
                        body += f"\n\n(+{extra} flere)"

                    bad = is_bad_turn(changes)
                    if hot and not bad:
                        head, pr, tag = "BILLETSIGNAL: ", prio, "rotating_light"
                    elif hot and bad:
                        head, pr, tag = "Lukket: ", 2, "no_entry"
                    else:
                        head, pr, tag = "Aendring: ", 1, "pencil"

                    push(
                        head + label,
                        f"{meta['name']}\n{meta['date']}\n\n{body}",
                        priority=pr, tags=tag, click=meta["url"] or None,
                    )
                    sent += 1
            else:
                log(f"  =   {label:34s} onsale={meta['onsale']:9s} {price_summary(ev)}")

        state[eid] = {
            "label": label, "city": city, "date": meta["date"], "url": meta["url"],
            "onsale": meta["onsale"], "price": price_summary(ev),
            "flat": flat,
            "seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    # Events der forsvandt helt fra Discovery
    gone = [e for e in state if e not in events and e in TARGETS]
    for eid in gone:
        log(f"  VAEK {state[eid].get('label', eid)} - ikke i svaret")

    save_state(state)
    return sent


def mode_diag():
    DEBUG_DIR.mkdir(exist_ok=True)
    events = collect()
    if not events:
        die("Ingen events. Tjek TM_API_KEY.")
    (DEBUG_DIR / "discovery_full.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n{len(events)} events\n" + "=" * 78)
    for eid, ev in events.items():
        meta = describe(ev)
        city, variant, _ = TARGETS.get(eid, (meta["city"], "ukendt", 2))
        print(f"\n{city} {meta['date']} ({variant})  [{eid}]")
        print(f"  {meta['name']}")
        print(f"  onsale : {meta['onsale']}")
        print(f"  pris   : {price_summary(ev)}")
        flat = flatten(strip_noise(ev))
        hot = {k: v for k, v in flat.items() if is_hot(k)}
        print(f"  felter : {len(flat)} i alt, {len(hot)} tilgaengelighedsrelaterede:")
        for k, v in sorted(hot.items()):
            print(f"      {k} = {fmt(v)}")
    print(f"\nFuldt dump: {DEBUG_DIR}/discovery_full.json")
    print("\nSe efter felter der kunne afsloere resale. Findes de ikke,")
    print("er Discovery API blind for resale, og vi maa laese selve siden.")


def mode_watch():
    state = load_state()
    baseline = not state
    deadline = time.time() + LOOP_MINUTES * 60
    p = 0
    total = 0

    log(f"Discovery-watch startet. {LOOP_MINUTES} min, interval {PASS_INTERVAL}s, topic {NTFY_TOPIC}")
    if baseline:
        log("Ingen state - foerste koersel er baseline (ingen alarmer)")

    while time.time() < deadline:
        p += 1
        log(f"--- pass {p} ---")
        total += run_pass(state, notify=not baseline)

        if baseline:
            lines = [
                f"{v['label']}: {v['onsale']} - {v['price']}"
                for v in sorted(state.values(), key=lambda x: x.get("date") or "")
            ]
            push("Discovery-watcher er i luften",
                 "Baseline:\n\n" + "\n".join(lines), priority=2, tags="white_check_mark")
            baseline = False

        if deadline - time.time() <= PASS_INTERVAL:
            break
        time.sleep(PASS_INTERVAL)

    log(f"Afsluttet efter {p} pass. {total} notifikationer.")


def main():
    if not API_KEY:
        die("TM_API_KEY mangler.")
    mode = (sys.argv[1] if len(sys.argv) > 1 else "watch").lower()
    if mode == "diag":
        mode_diag()
    elif mode == "watch":
        mode_watch()
    else:
        die(f"Ukendt mode '{mode}'. Brug: diag | watch")


if __name__ == "__main__":
    main()

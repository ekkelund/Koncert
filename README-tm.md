# Ticketmaster Resale Watcher

Overvaager Ticketmasters **resale**-marked (og primaermarkedet) for The Neighbourhood
THE WOURLD TOUR 2026 og pusher til iPhone via ntfy.sh.

Maal: Koeln, LANXESS arena, 30. august 2026. Hamburg 29/8, Paris 8/9 og Madrid 10/9
overvaages som backup.

## Hvorfor ikke Playwright denne gang

Groen-watcheren scrapede Billettens widget med headless Chromium. Det duer ikke her:
Ticketmaster koerer aggressiv bot-detektion, og GitHub Actions' datacenter-IP'er bliver
udfordret med captcha. I stedet bruger vi Ticketmasters **officielle** API'er. Ingen
scraping, ingen ToS-graazone, ingen kalibreringshelvede.

Kaeden er to kald:

| Trin | Endpoint | Giver |
|---|---|---|
| 1 | `discovery/v2/events.json?keyword=...&countryCode=DE` | universal event-IDs, by, dato, URL |
| 2 | `inventory-status/v1/availability?events=id1,id2` | `status` + **`resaleStatus`** pr. event |

`resaleStatus` er hele pointen. Det er et separat felt fra primaerstatus, og det er
praecis det signal Ticketmasters website *ikke* giver dig en notifikation paa.

## De to knapper paa Ticketmaster-siden - hvad de faktisk goer

- **"Add to waiting list"** = marketing-mail fra Live Nation. Varsler nye udslip paa
  **primaer**markedet (frigivne production holds, ekstra sektioner, nye tourdatoer).
  Siger intet om resale.
- **"Resale tickets will be displayed below as soon as they become available"** = ren
  passiv visning. Ingen mail, ingen push, ingen koe. Du skal selv staa med fingeren paa
  F5 i det rigtige sekund. Det hul lukker denne watcher.

## Opsaetning

### 1. API-noegle

1. Opret konto paa https://developer.ticketmaster.com
2. Default-appen oprettes automatisk. Kopiér **Consumer Key** - det er din API-noegle.
3. GitHub: repo -> Settings -> Secrets and variables -> Actions -> New repository secret
   - Navn: `TM_API_KEY`
   - Vaerdi: din Consumer Key

> Noeglen maa **ikke** committes til repoet. Repoet er public.

### 2. ntfy

1. Installér **ntfy** fra App Store
2. Abonnér paa emnet: `tm-ekkelund-billet-4412`
3. Tillad kritiske notifikationer, ellers rammer prioritet 5 ikke gennem Fokus

### 3. Workflow-fil

GitHub-connectoren kan ikke skrive til `.github/workflows/`, saa denne fil skal du selv
oprette i GitHub-web-UI'et. Se `WORKFLOW-tm.yml.txt` i repoet - kopiér indholdet ind i
`.github/workflows/tm-watch.yml`.

## Koersel

### Foerste skridt: find event-IDs

Actions -> **TM Resale Watch** -> Run workflow -> saet `mode` til `discover`.

Output er en tabel med dato, by, event-ID og onsale-status. Tjek at Koeln 2026-08-30
er med. Bemaerk: der findes **to** Koeln-events den dag - hovedeventet og en separat
"Box-Seat"-variant. Begge fanges automatisk, fordi vi soeger paa keyword og ikke paa ét ID.

### Andet skridt: verificér at resaleStatus faktisk udfyldes

Koer med `mode` = `diag`. Den dumper det raa JSON-svar til `debug/inventory.json` som
artifact.

**Dette er det kritiske tjek.** Ticketmasters dokumentation angiver at *prisdata* i
Inventory Status API kun understoettes i US, CA, AU, NZ og MX. Om `resaleStatus`-feltet
udfyldes for tyske events er ikke dokumenteret. Diag-koerslen svarer paa det.

- Kommer der `resaleStatus` med? Saa er vi i maal.
- Kommer feltet tomt eller helt uden? Saa falder scriptet tilbage paa primaerstatus,
  og vi maa tage stilling til plan B (lokal koersel fra dansk IP i stedet for Actions).

### Tredje skridt: lad den koere

`mode` = `watch` (default). Cron starter en ny koersel hver time. Hver koersel loeber
50 minutter med et inventory-kald pr. minut, og laver kun et nyt discovery-kald hvert
20. pass (event-IDs skifter ikke).

Foerste koersel er **baseline**: den registrerer nuvaerende status uden at alarmere, og
sender én stille bekraeftelse paa at maskinen er i luften.

## Signal-logik

Der pushes **kun** ved skift fra "ikke tilgaengelig" til "tilgaengelig". Ingen
gentagelser saa laenge status er uaendret. Lukker resale og aabner igen, kommer der ny
alarm.

| Situation | Push |
|---|---|
| Baseline-koersel | Én stille bekraeftelse (prioritet 2) |
| `resaleStatus` flipper til available | **Alarm.** Koeln = prioritet 5 |
| `status` (primaer) flipper til available | Alarm, samme prioritet |
| Uaendret | Intet |
| Lukker igen | Intet (men state opdateres, saa naeste aabning alarmerer) |

## Kvote

Discovery API's standardkvote er 5.000 kald/dag. Med 1 inventory-kald pr. minut plus
et discovery-kald hvert 20. minut lander vi paa ca. 1.500 kald/dag. God margin.
Scriptet logger `Ratelimit-Quota-Available` naar headeren er der.

## Bevidste ikke-features

- **Intet autokoeb.** Samme konklusion som ved Groen: 3D Secure og betalingskort i et
  offentligt repo er en daarlig idé, og det bryder Ticketmasters vilkaar. Watcheren
  vaekker dig - du koeber selv.
- **Ingen sektions- eller prisfiltrering.** Inventory Status API giver et binaert
  signal, ikke listings. Du faar besked om at *der er noget* - ikke hvad.

## Timing

Koeln er 30. august 2026. Saet en auto-nedlukning ind naar datoen naermer sig, ellers
koerer den for evigt (den fejl kostede os Actions-minutter sidste gang).

## Stop overvaagningen

Slet `.github/workflows/tm-watch.yml` eller disable workflowet under Actions.

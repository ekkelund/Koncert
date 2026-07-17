# Grøn Resale Watcher

Overvåger [Grøn Koncerts Resale-markedsplads](https://groenkoncert.dk/billetter/) for **Odense (24/7), Næstved (25/7) og Valby (26/7)** og sender push-notifikation via [ntfy.sh](https://ntfy.sh), når der sker ændringer.

## Sådan virker det

- GitHub Actions kører `groen_watch.py` hvert 10. minut (cron, UTC)
- Scriptet åbner billetsiden i headless Chromium, klikker "Køb Resale-billetter" for hver by og læser indholdet
- Hvis indholdet ikke matcher "tom markedsplads"-mønstrene OG har ændret sig siden sidst, sendes en push
- Status gemmes i `groen_state.json` (committes automatisk af workflow)

## Push-notifikationer på iPhone

1. Installér **ntfy** fra App Store
2. Abonnér på emnet: `groen-ekkelund-billet-7391`
3. Færdig, notifikationer kommer automatisk

> Emnet er offentligt hos ntfy.sh. Skift det i `groen_watch.py`, hvis du vil have et andet.

## Kalibrering efter første kørsel

Hver kørsel uploader et debug-dump pr. by som artifact (Actions → seneste run → Artifacts). Tjek hvad Billetten-widgetten faktisk skriver, når der er 0 billetter, og justér `NO_TICKETS_PATTERNS` i `groen_watch.py`, hvis ordlyden afviger.

## Manuel kørsel

Actions-fanen → **Groen Resale Watch** → **Run workflow**

## Stop overvågningen

Slet `.github/workflows/watch.yml` eller disable workflowet under Actions (gør det efter Valby 26/7, ellers kører den for evigt).

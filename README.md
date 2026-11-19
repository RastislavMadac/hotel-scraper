# Desktop Hotel Price Scraper (Zero-Update Architecture)

Autonómny modulárny scraper pre porovnávanie cien ubytovania medzi **Booking.com** a **priamymi webmi hotelov** (napr. Profitroom, Hoteladmin, custom enginy).

---

## Kľúčové architektonické princípy

1. **Zero-Update politika:**
   - Žiadne selektory, regexy, timeouty ani cieľové hotely nie sú natvrdo v kóde.
   - Všetko sa sťahuje dynamicky zo Supabase tabuliek (`scraping_configs`, `target_hotels`) za behu.
   - Pri výpadku siete existuje automatický fallback na lokálnu persistentnú JSON kešku (`.cache/config_cache.json`, `.cache/hotels_cache.json`).
2. **Striktné logovanie (zákaz `print()`):**
   - V celom projekte sa používa výhradne `loguru.logger`.
   - Záznamy úrovní `WARNING`, `ERROR` a `CRITICAL` sú asynchrónne a neblokujúco odosielané do Supabase tabuľky `app_logs` vrátane fingerprintu stroja (`machine_id`) a stack traceu.
   - Lokálny rotujúci fallback ukladá logy do `logs/app.log` (5 MB rotácia, 3 dni retencia, kompresia).
3. **Defenzívne spracovanie:**
   - Pád extrakcie jedného hotela (napr. zmena DOM, timeout) nezastaví celý proces – scraper incident zaloguje a pokračuje ďalším hotelom.
   - Automatické ukladanie diagnostických screenshotov pri neznámom layoute do `logs/screenshots/`.
4. **Thread Safety & UI Responsiveness:**
   - Playwright scraper beží vo vyhradenom worker vlákne (`ScraperWorkerThread`), čím neblokuje UI ani CLI.
   - Komunikácia prebieha cez thread-safe frontu `queue.Queue`.
5. **Anti-Bot stealth:**
   - Chromium beží s `playwright-stealth` a upravenými prepínačmi.
   - Náhodný jitter (pauza 1.5s – 3.5s) medzi požiadavkami.
   - Rotácia reálnych desktopových User-Agentov.
6. **Excel Reportér:**
   - Export do formátovaného `.xlsx` (Pandas + OpenPyXL) so zvýraznením delty cien:
     - **Zelené podfarbenie:** Priamy web je lacnejší (`Delta < 0`).
     - **Červené podfarbenie:** Booking je lacnejší (`Delta > 0`).
     - **Modré podfarbenie:** Rovnaká cena.

---

## Modulárna štruktúra

```
hotels_scraper/
├── config/
│   └── settings.py          # Konfigurácia, runtime cesty, .env načítanie
├── core/
│   ├── models.py            # ScrapingJob, ScrapedResult, ResultStatus
│   ├── config_manager.py    # Zero-Update sťahovanie pravidiel a hotelov s lokálnou keškou
│   └── logger.py            # Loguru konfigurácia a neblokujúci Supabase sink
├── scraper/
│   ├── browser.py           # Playwright Chromium lifecycle s playwright-stealth
│   ├── engine.py            # Hlavný orchestrátor, defenzívna slučka, jitter
│   ├── booking_parser.py    # Booking.com parser s detekciou vypredanosti (Sold Out)
│   └── direct_parser.py     # Parser priamych webov hotelov (Profitroom, hoteladmin, custom)
├── exporters/
│   └── excel_generator.py   # Excel reportér (Pandas + OpenPyXL) s farebným zvýraznením
├── services/
│   └── queue_manager.py     # Thread-safe zbernica udalostí a rušenie úloh
├── ui_bridge/
│   └── worker.py            # ScraperWorkerThread pre beh na pozadí
├── tests/                   # 57 unit a integračných testov
├── main.py                  # Autonómny CLI runner
├── supabase_schema.sql      # SQL schéma a seed dáta pre Supabase
└── requirements.txt         # Zoznam závislostí
```

---

## Inštalácia a spustenie

### 1. Inštalácia závislostí
```bash
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\playwright install chromium
```

### 2. Konfigurácia prostredia
Skopírujte šablónu `.env.example` do `.env` a nastavte prístupy k Supabase:
```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-anon-or-service-role-key
```
*(Poznámka: Ak Supabase nenastavíte, scraper beží automaticky v offline režime z lokálnej kešky v `.cache/`)*.

### 3. Spustenie CLI runnera
```bash
# Predvolený beh (zajtrajší príchod na 2 noci pre 2 osoby v headless režime):
.\.venv\Scripts\python.exe main.py

# Beh s vlastnými parametrami:
.\.venv\Scripts\python.exe main.py --check-in 2026-12-01 --check-out 2026-12-05 --guests 2 --output-dir reports

# Výber konkrétnych hotelov:
.\.venv\Scripts\python.exe main.py --hotels grand_hotel_kempinski,hotel_marrols

# Spustenie s viditeľným oknom prehliadača:
.\.venv\Scripts\python.exe main.py --no-headless
```

### 4. Spustenie testovacej sady
```bash
.\.venv\Scripts\python.exe -m unittest discover tests
```
Všetkých 57 unit a integračných testov prebehne za ~5 sekúnd s kompletným overením všetkých komponentov.


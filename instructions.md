# Hotel Scraper Architect & Implementation Guide
A technical blueprint and coding guideline for developing a modular, zero-update desktop hotel price scraper.

## Core Architectural Rules
1. Zero-Update Philosophy:
   - Never hardcode CSS selectors, target hotel URLs, or timeout values in Python files.
   - Fetch all selectors, regex patterns, and hotel configurations from Supabase tables (scraping_configs, target_hotels) at runtime.
   - Support generic JSON-driven field parsing (fields_schema).

2. Strict Logging Protocol:
   - Absolute ban on print() calls. Use loguru.logger exclusively.
   - Implement a custom asynchronous/non-blocking Loguru sink pushing WARNING, ERROR, and CRITICAL records to the Supabase app_logs table.
   - Fall back to a local rotating file log (logs/app.log) if offline.

3. Defensive Processing:
   - Wrap every single hotel and field parsing routine in isolated try-except blocks.
   - An individual hotel extraction failure must log the incident and continue with remaining targets without terminating the scraping loop.

4. Thread Safety & UI Responsiveness:
   - Decouple the UI thread from the Playwright scraping engine using threading.Thread and queue.Queue.
   - Send granular progress updates (percentage, current hotel, warnings) to the queue for UI consumption.

5. Data Flow & Output:
   - Normalize inputs into an immutable ScrapingJob dataclass (frozen=True).
   - Export paired price comparisons (Booking.com vs. Direct hotel site, calculated delta) into an .xlsx file using Pandas/OpenPyXL.

## Module Structure
hotels_scraper/
├── config/settings.py          # Environment, Supabase keys, runtime paths
├── core/
│   ├── models.py              # ScrapingJob, ScrapedResult dataclasses
│   ├── config_manager.py      # Supabase remote rule retrieval & caching
│   └── logger.py              # Loguru setup with Supabase async sink
├── scraper/
│   ├── browser.py             # Playwright stealth context, viewport, headers
│   ├── engine.py              # Main loop, delays, queue dispatch
│   ├── booking_parser.py      # Dynamic Booking.com extractor
│   └── direct_parser.py       # Direct booking engine extractor (Profitroom, hoteladmin, custom)
├── exporters/excel_generator.py # Excel builder with delta highlighting
├── services/queue_manager.py  # Thread-safe queue communication
├── ui_bridge/                 # Worker management and frontend bridge
├── tests/                     # Verification tests
└── main.py                    # Application entry point

## Anti-Bot Best Practices
- Run Playwright with playwright-stealth.
- Inject randomized jitter pauses between requests (1.5s - 4.0s).
- Rotate standard desktop User-Agents.
- Intercept network responses (page.wait_for_response) where API or HTML fragments are available to bypass DOM overhead.

--------------------------------------------------
ÚLOHA:
Začni realizáciu projektu. Vytvor súbor `core/models.py` s nemennou triedou `ScrapingJob` (@dataclass(frozen=True)), kompletnou defenzívnou validáciou vstupov a štruktúrou pre výsledky `ScrapedResult`.
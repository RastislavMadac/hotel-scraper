"""Autonomous CLI runner entry point for the Zero-Update desktop hotel price scraper."""

import argparse
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import queue
import sys
import time
from typing import List, Optional

from config.settings import get_settings
from core.config_manager import ConfigManager
from core.logger import logger
from core.models import ScrapingJob
from exporters.excel_generator import ExcelReportGenerator
from scraper.browser import BrowserManager
from scraper.engine import ScrapingEngine
from ui_bridge.worker import ScraperWorkerThread


def parse_arguments(cli_args: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments with default options and validations."""
    tomorrow = date.today() + timedelta(days=1)
    default_checkout = tomorrow + timedelta(days=2)  # check-in + 2 dni

    parser = argparse.ArgumentParser(
        description="Autonómny Zero-Update scraper cien hotelov (Booking.com vs. Priamy web).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--check-in",
        "--checkin",
        dest="check_in",
        type=str,
        default=tomorrow.strftime("%Y-%m-%d"),
        help="Dátum príchodu vo formáte YYYY-MM-DD (default: zajtrajší dátum).",
    )
    parser.add_argument(
        "--check-out",
        "--checkout",
        dest="check_out",
        type=str,
        default=default_checkout.strftime("%Y-%m-%d"),
        help="Dátum odchodu vo formáte YYYY-MM-DD (default: check-in + 2 dni).",
    )
    parser.add_argument(
        "--guests",
        "--adults",
        dest="guests",
        type=int,
        default=2,
        help="Počet dospelých hostí (default: 2).",
    )
    parser.add_argument(
        "--children",
        type=int,
        default=0,
        help="Počet detí (default: 0).",
        
    )
    parser.add_argument(
        "--child-ages",
        nargs="*",
        type=int,
        default=[],
        help="Veky jednotlivých detí oddelené medzerou (napr. --child-ages 6 alebo --child-ages 4 8).",
    )
    parser.add_argument(
        "--email",
        type=str,
        default=None,
        help="Cieľový notifikačný email (voliteľné).",
    )
    parser.add_argument(
        "--hotels",
        type=str,
        default=None,
        help="Zoznam ID alebo názvov hotelov oddelených čiarkou (default: všetky zo Supabase).",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="reports",
        help="Cieľový adresár pre generované Excel reporty (default: reports).",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Spustiť Chromium prehliadač v headless režime.",
    )
    parser.add_argument(
        "--min-jitter",
        type=float,
        default=1.5,
        help="Minimálna stealth pauza v sekundách medzi requestami.",
    )
    parser.add_argument(
        "--max-jitter",
        type=float,
        default=3.5,
        help="Maximálna stealth pauza v sekundách medzi requestami.",
    )
    parser.add_argument(
        "--sync-rooms",
        action="store_true",
        default=False,
        help="Spustiť bezdátumovú katalógovú synchronizáciu izieb a mapovaní do hs-room_mappings.",
    )
    parser.add_argument(
        "--export-db-only",
        action="store_true",
        default=False,
        help="Vygenerovať surový Excel report priamo z databázy (hs-price_comparisons) bez spustenia prehliadača.",
    )

    parsed = parser.parse_args(cli_args)
    setattr(parsed, "adults", parsed.guests)
    return parsed


def main(cli_args: Optional[List[str]] = None) -> int:
    """Main CLI execution routine with full defensive exception handling."""
    logger.info("Spúšťam autonómny CLI runner hotelového scrapera...")

    try:
        args = parse_arguments(cli_args)

        # 1. Filter target hotels if specified
        hotel_identifiers: Optional[List[str]] = None
        if args.hotels:
            hotel_identifiers = [h.strip() for h in args.hotels.split(",") if h.strip()]

        # 2. Date-independent room catalog synchronization mode (--sync-rooms)
        if getattr(args, "sync_rooms", False):
            logger.info("Spúšťam katalógovú synchronizáciu izieb (--sync-rooms)...")
            config_manager = ConfigManager()
            browser_manager = BrowserManager(
                headless=args.headless,
                timeout_ms=get_settings().default_timeout_ms,
            )
            engine = ScrapingEngine(
                config_manager=config_manager,
                browser_manager=browser_manager,
                min_jitter_sec=args.min_jitter,
                max_jitter_sec=args.max_jitter,
            )
            synced_count = engine.sync_room_catalog(hotel_identifiers=hotel_identifiers)
            logger.info("=" * 60)
            logger.info("KATALÓGOVÁ SYNCHRONIZÁCIA IZIEB DOKONČENÁ:")
            logger.info(f"  - Počet synchronizovaných mapovaní izieb: {synced_count}")
            logger.info(f"  - Filtre hotelov: {hotel_identifiers or 'všetky'}")
            logger.info("=" * 60)
            return 0

        # 2b. Export raw Excel directly from Supabase hs-price_comparisons without browser
        if getattr(args, "export_db_only", False):
            logger.info("Spúšťam surový Excel export priamo z DB (--export-db-only)...")
            config_manager = ConfigManager()
            db_rows = config_manager.fetch_all_price_comparisons()
            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            report_path = output_dir / f"hotel_prices_db_{timestamp}.xlsx"
            generated_path = ExcelReportGenerator(exports_dir=output_dir).generate_raw_db_report(
                rows=db_rows,
                output_path=report_path,
            )
            logger.info("=" * 60)
            logger.info("SUROVÝ EXCEL EXPORT Z DATABÁZY DOKONČENÝ:")
            logger.info(f"  - Počet záznamov z DB: {len(db_rows)}")
            logger.info(f"  - Vygenerovaný Excel report: {generated_path}")
            logger.info("=" * 60)
            return 0

        # 3. Parse and validate dates
        try:
            check_in_date = datetime.strptime(args.check_in, "%Y-%m-%d").date()
            check_out_date = datetime.strptime(args.check_out, "%Y-%m-%d").date()
        except ValueError as exc:
            logger.error(f"Neplatný formát dátumu (musí byť YYYY-MM-DD): {exc}")
            return 1

        # 4. Create validated ScrapingJob
        # 3. Create validated ScrapingJob
        try:
            # Extrakcia zoznamu vekov z CLI
            child_ages: List[int] = list(getattr(args, "child_ages", []) or [])

            # Fallback: ak používateľ zadal starší prepínač --children bez špecifikácie vekov
            if not child_ages and getattr(args, "children", 0) > 0:
                child_ages = [6] * args.children

            total_children = len(child_ages)

            job = ScrapingJob(
                check_in=check_in_date,
                check_out=check_out_date,
                adults=args.guests,
                children=total_children,
                children_ages=child_ages,
                email=args.email,
                hotel_identifiers=hotel_identifiers,
            )
        except (ValueError, TypeError) as exc:
            logger.error(f"Validácia vstupných parametrov zlyhala: {exc}")
            return 1

        logger.info(
            f"Vytvorená úloha: {job.check_in} -> {job.check_out} ({job.nights} nocí), "
            f"{job.adults} dospelí, filtre hotelov: {job.hotel_identifiers or 'všetky'}"
        )

        # 4. Initialize ConfigManager, Queue, and BrowserManager
        config_manager = ConfigManager()
        progress_queue: queue.Queue = queue.Queue()
        browser_manager = BrowserManager(
            headless=args.headless,
            timeout_ms=get_settings().default_timeout_ms,
        )

        # 5. Launch background ScraperWorkerThread
        worker = ScraperWorkerThread(
            job=job,
            config_manager=config_manager,
            progress_queue=progress_queue,
            output_dir=args.output_dir,
            browser_manager=browser_manager,
        )
        worker.start()

        # 6. Monitor progress queue
        report_file_path: Optional[str] = None
        execution_failed = False
        failure_reason: Optional[str] = None

        while True:
            try:
                msg = progress_queue.get(timeout=0.5)
                status = msg.get("status")

                if status == "starting":
                    logger.info(msg.get("message", "Inicializácia zberu dát..."))
                elif status == "running":
                    curr = msg.get("current")
                    tot = msg.get("total")
                    hotel = msg.get("hotel")
                    pct = msg.get("percent")
                    logger.info(f"Priebeh: {pct}% | Spracovávam: {hotel} ({curr}/{tot})")
                elif status == "finished":
                    report_file_path = msg.get("report_path")
                elif status == "failed":
                    execution_failed = True
                    failure_reason = msg.get("error")
                    logger.critical(f"Chyba pri zbere dát: {failure_reason}")
                elif status == "stopped":
                    logger.warning(msg.get("message", "Zber dát bol prerušený."))
                elif status == "idle":
                    # Finished processing
                    break

            except queue.Empty:
                if not worker.is_alive():
                    break

        worker.join()

        if execution_failed:
            logger.critical(f"Beh scrapera zlyhal: {failure_reason}")
            return 1

        # 7. Export raw Excel report directly from DB and verify summary statistics
        results = worker.results
        successful_count = sum(
            1 for r in results if r.booking_price is not None or r.direct_price is not None
        )
        failed_count = len(results) - successful_count

        resolved_report = report_file_path or worker.report_path
        has_existing_report = (
            isinstance(resolved_report, (str, Path)) and Path(resolved_report).exists()
        )
        if len(results) > 0 and not has_existing_report:
            try:
                db_rows = config_manager.fetch_all_price_comparisons()
                if isinstance(db_rows, list) and (len(db_rows) > 0 or len(results) > 0):
                    if isinstance(resolved_report, (str, Path)) and str(resolved_report).strip():
                        target_db_path = Path(resolved_report)
                    else:
                        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                        target_db_path = Path(args.output_dir) / f"hotel_prices_db_{timestamp}.xlsx"
                    generated_db_report = ExcelReportGenerator(
                        exports_dir=args.output_dir
                    ).generate_raw_db_report(
                        rows=db_rows,
                        output_path=target_db_path,
                    )
                    resolved_report = str(generated_db_report)
            except Exception as exc:
                logger.warning(
                    f"Nepodarilo sa vygenerovať surový DB Excel report: {exc}"
                )

        if isinstance(resolved_report, (str, Path)) and Path(resolved_report).exists():
            logger.info("=" * 60)
            logger.info("FINÁLNE ZHRNUTIE ZBERU DÁT:")
            logger.info(f"  - Celkový počet hotelov: {len(results)}")
            logger.info(f"  - Úspešne vyťažené: {successful_count}")
            logger.info(f"  - Neúspešné / Nedostupné: {failed_count}")
            logger.info(f"  - Vygenerovaný Excel report: {resolved_report}")
            logger.info("=" * 60)
            return 0
        elif len(results) == 0:
            logger.warning("Neboli nájdené žiadne cieľové hotely na spracovanie.")
            return 0
        else:
            logger.error(f"Očakávaný report v '{args.output_dir}' nebol nájdený!")
            return 1

    except Exception as exc:
        # Critical top-level handler pushes fatal trace to Supabase app_logs sink
        logger.opt(exception=True).critical(f"Kritická neočakávaná chyba v main(): {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

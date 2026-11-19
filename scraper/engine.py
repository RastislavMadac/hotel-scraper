"""Main scraping orchestrator engine executing hotel price comparisons."""

from datetime import date, datetime, timedelta
from pathlib import Path
import queue
import random
import time
from typing import Any, Dict, List, Optional, Union

from playwright.sync_api import Page

from config.settings import get_settings
from core.config_manager import ConfigError, ConfigManager
from core.logger import logger
from core.models import ResultStatus, RoomOffer, ScrapedResult, ScrapingJob
from core.room_matcher import RoomMatcher
from exporters.excel_generator import ExcelReportGenerator
from scraper.booking_parser import BookingParser
from scraper.browser import BrowserManager
from scraper.direct_parser import DirectParser


class ScrapingEngine:
    """Orchestrates end-to-end hotel price scraping across Booking.com and direct websites."""

    def __init__(
        self,
        job: Optional[ScrapingJob] = None,
        config_manager: Optional[ConfigManager] = None,
        progress_queue: Optional[Any] = None,
        browser_manager: Optional[BrowserManager] = None,
        min_jitter_sec: Optional[float] = None,
        max_jitter_sec: Optional[float] = None,
        stop_event: Optional[Any] = None,
        output_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """Initialize ScrapingEngine.

        Args:
            job: Optional ScrapingJob defining stay dates, guests, and target hotels.
            config_manager: Injected ConfigManager for remote rules retrieval.
            progress_queue: Optional queue.Queue or QueueManager for UI communication.
            browser_manager: Optional custom or mock BrowserManager instance.
            min_jitter_sec: Minimum stealth pause between hotel requests.
            max_jitter_sec: Maximum stealth pause between hotel requests.
            stop_event: Optional threading.Event to abort execution gracefully.
        """
        settings = get_settings()

        if job is None:
            today = date.today()
            job = ScrapingJob(check_in=today, check_out=today + timedelta(days=1))

        self.job = job
        self.config_manager = config_manager or ConfigManager()
        self.progress_queue = progress_queue
        self.browser_manager = browser_manager or BrowserManager()
        self.min_jitter = (
            min_jitter_sec if min_jitter_sec is not None else settings.min_jitter_sec
        )
        self.max_jitter = (
            max_jitter_sec if max_jitter_sec is not None else settings.max_jitter_sec
        )
        self.stop_event = stop_event
        self.output_path = output_path
        self.last_report_path: Optional[str] = None

        self.booking_parser = BookingParser()
        self.direct_parser = DirectParser()
        self.room_matcher = RoomMatcher()
        self._rules_cache: Dict[str, Dict[str, Any]] = {}
        self._in_run_loop: bool = False

    def _send_progress(self, payload: Dict[str, Any]) -> None:
        """Safely deliver progress notifications to the UI queue."""
        if self.progress_queue is None:
            return
        try:
            if hasattr(self.progress_queue, "put"):
                self.progress_queue.put(payload)
            elif hasattr(self.progress_queue, "publish"):
                self.progress_queue.publish(payload)
        except Exception as exc:
            logger.warning(f"Failed to post to progress_queue: {exc}")

    def _get_rules(self, engine_name: str) -> Dict[str, Any]:
        """Fetch and cache scraping rules for the specified engine."""
        if engine_name not in self._rules_cache:
            try:
                self._rules_cache[engine_name] = self.config_manager.get_scraping_rules(
                    engine_name
                )
            except Exception as exc:
                logger.warning(
                    f"Could not load scraping rules for engine '{engine_name}': {exc}"
                )
                self._rules_cache[engine_name] = {}
        return self._rules_cache[engine_name]

    def _scrape_single_hotel(
        self,
        hotel: Dict[str, Any],
        page: Page,
        booking_rules: Dict[str, Any],
    ) -> ScrapedResult:
        """Scrape Booking.com and direct website for one hotel in isolated try-except."""
        hotel_id = str(hotel.get("id", ""))
        hotel_name = str(hotel.get("name", "Unknown Hotel"))
        base_booking_url = hotel.get("booking_url", "")
        base_direct_url = hotel.get("direct_url", "")
        direct_engine = hotel.get("direct_booking_engine", "generic")

        resolved_booking_url = ""
        resolved_direct_url = ""
        booking_result: Optional[ScrapedResult] = None
        direct_result: Optional[ScrapedResult] = None
        booking_error_message: Optional[str] = None
        direct_error_message: Optional[str] = None
        error_messages: List[str] = []

        # 1. Scrape Booking.com
        if base_booking_url:
            try:
                resolved_booking_url = self.booking_parser.build_booking_url(
                    base_booking_url, self.job
                )
                logger.debug(f"Navigating to Booking: {resolved_booking_url}")
                page.goto(resolved_booking_url, wait_until="domcontentloaded")
                booking_result = self.booking_parser.parse_hotel_page(
                    page=page,
                    rules=booking_rules,
                    hotel_name=hotel_name,
                    requested_guests=self.job.total_guests,
                    job=self.job,
                )
                if booking_result.error_message:
                    booking_error_message = booking_result.error_message
                    error_messages.append(f"Booking: {booking_result.error_message}")
            except Exception as exc:
                logger.error(f"Error scraping Booking.com for {hotel_name}: {exc}")
                booking_error_message = str(exc)
                error_messages.append(f"Booking error: {exc}")

        # 2. Scrape Direct Website
        if base_direct_url:
            try:
                direct_rules = self._get_rules(direct_engine)
                logger.info(
                    f"Spúšťam Direct pre '{hotel_name}' s job: {self.job.check_in} -> {self.job.check_out}"
                )

                resolved_direct_url = self.direct_parser.build_direct_url(
                    base_direct_url, self.job, direct_rules
                )
                logger.debug(f"Navigating to Direct: {resolved_direct_url}")
                page.goto(resolved_direct_url, wait_until="domcontentloaded")

                direct_result = self.direct_parser.parse_direct_hotel(
                    page=page,
                    rules=direct_rules,
                    hotel_name=hotel_name,
                    direct_url=resolved_direct_url,
                    job=self.job,
                )
                if direct_result.error_message:
                    direct_error_message = direct_result.error_message
                    error_messages.append(f"Direct: {direct_result.error_message}")
            except Exception as exc:
                logger.error(f"Error scraping direct site for {hotel_name}: {exc}")
                direct_error_message = str(exc)
                error_messages.append(f"Direct error: {exc}")

        # 3. Consolidate into merged ScrapedResult & apply saved room mappings
        b_price = booking_result.booking_price if booking_result else None
        d_price = direct_result.direct_price if direct_result else None
        raw_booking_offers = list(booking_result.offers) if booking_result else []
        raw_direct_offers = list(direct_result.direct_offers) if direct_result else []

        all_offers = [
            o
            for o in raw_booking_offers
            if getattr(o, "room_name", None) and str(o.room_name).strip() not in ("", "—")
        ]
        direct_offers = [
            o
            for o in raw_direct_offers
            if getattr(o, "room_name", None) and str(o.room_name).strip() not in ("", "—")
        ]

        if booking_result is not None:
            if not booking_result.is_available or (raw_booking_offers and not all_offers):
                b_price = None
            elif len(all_offers) < len(raw_booking_offers):
                valid_b_prices = [
                    o.price for o in all_offers if o.price is not None and o.price > 0
                ]
                b_price = min(valid_b_prices) if valid_b_prices else None

        if direct_result is not None:
            if not direct_result.is_available or (raw_direct_offers and not direct_offers):
                d_price = None
            elif len(direct_offers) < len(raw_direct_offers):
                valid_d_prices = [
                    o.price for o in direct_offers if o.price is not None and o.price > 0
                ]
                d_price = min(valid_d_prices) if valid_d_prices else None

        try:
            saved_mappings = self.config_manager.get_room_mappings(hotel_id)
            if isinstance(saved_mappings, list) and saved_mappings:
                all_offers, direct_offers = self.room_matcher.apply_saved_mappings(
                    all_offers, direct_offers, saved_mappings
                )
        except Exception as exc:
            logger.warning(f"Failed to apply saved room mappings for '{hotel_name}': {exc}")

        currency = (
            (booking_result.currency if booking_result else None)
            or (direct_result.currency if direct_result else None)
            or "EUR"
        )

        # Availability logic
        booking_avail = bool(
            booking_result and booking_result.is_available and b_price is not None
        )
        direct_avail = bool(
            direct_result and direct_result.is_available and d_price is not None
        )
        is_avail = booking_avail or direct_avail

        # Status determination
        if b_price is not None and d_price is not None:
            status = ResultStatus.SUCCESS
        elif b_price is not None or d_price is not None:
            status = ResultStatus.PARTIAL
        elif not is_avail and (
            (booking_result and booking_result.status == ResultStatus.NOT_AVAILABLE)
            or (direct_result and direct_result.status == ResultStatus.NOT_AVAILABLE)
            or (booking_result is not None and bool(raw_booking_offers) and not all_offers)
            or (direct_result is not None and bool(raw_direct_offers) and not direct_offers)
        ):
            status = ResultStatus.NOT_AVAILABLE
        else:
            status = ResultStatus.FAILED

        combined_error = "; ".join(error_messages) if error_messages else None

        result = ScrapedResult(
            hotel_id=hotel_id,
            hotel_name=hotel_name,
            check_in=self.job.check_in,
            check_out=self.job.check_out,
            booking_price=b_price,
            direct_price=d_price,
            currency=currency,
            booking_url=resolved_booking_url or base_booking_url,
            direct_url=resolved_direct_url or base_direct_url,
            status=status,
            is_available=is_avail,
            offers=all_offers,
            direct_offers=direct_offers,
            error_message=combined_error,
            booking_error_message=booking_error_message,
            direct_error_message=direct_error_message,
        )

        if not self._in_run_loop:
            try:
                comparison_records = self.build_price_comparison_records([result])
                if comparison_records:
                    self.config_manager.upsert_price_comparisons(comparison_records)
            except Exception as exc:
                logger.warning(
                    f"Failed to upsert price comparisons for '{hotel_name}': {exc}"
                )

        return result

    @staticmethod
    def _resolve_offer_availability_status(
        direct_price: Optional[float], booking_price: Optional[float]
    ) -> str:
        """Determine channel-level availability status for a price comparison row."""
        if direct_price is not None and booking_price is not None:
            return "AVAILABLE"
        if direct_price is not None and booking_price is None:
            return "DIRECT_ONLY"
        if booking_price is not None and direct_price is None:
            return "BOOKING_ONLY"
        return "SOLD_OUT"

    def build_price_comparison_records(
        self, results: List[ScrapedResult]
    ) -> List[Dict[str, Any]]:
        """Assemble normalized per-channel hs-price_comparisons records (1 row = 1 room on Direct or Booking)."""
        deduped: Dict[tuple, Dict[str, Any]] = {}

        def _register_record(record: Dict[str, Any]) -> None:
            key = (
                record["scraped_date"],
                record["hotel_id"],
                record["check_in"],
                record["check_out"],
                record["adults"],
                record["children"],
                record["children_ages"],
                record["channel"],
                record["room_name"],
                record["price"],
            )
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = record
                return
            if not existing.get("bonuses") and record.get("bonuses"):
                deduped[key] = record

        for res in results:
            scraped_dt = (
                res.scraped_at
                if isinstance(res.scraped_at, datetime)
                else datetime.utcnow()
            )
            scraped_date_str = scraped_dt.date().isoformat()
            scraped_at_str = scraped_dt.isoformat(timespec="seconds")

            cin_date = res.check_in or self.job.check_in
            cout_date = res.check_out or self.job.check_out
            check_in_str = (
                cin_date.isoformat()
                if isinstance(cin_date, date)
                else str(cin_date or "")
            )
            check_out_str = (
                cout_date.isoformat()
                if isinstance(cout_date, date)
                else str(cout_date or "")
            )
            nights = (
                max((cout_date - cin_date).days, 1)
                if (isinstance(cin_date, date) and isinstance(cout_date, date))
                else self.job.nights
            )
            lead_time = (
                (cin_date - scraped_dt.date()).days
                if isinstance(cin_date, date)
                else 0
            )
            adults = int(self.job.adults)
            children = int(self.job.children)
            children_ages_str = ", ".join(
                str(a) for a in (self.job.children_ages or [])
            )

            direct_offers = [
                d_off
                for d_off in list(getattr(res, "direct_offers", None) or [])
                if getattr(d_off, "room_name", None)
                and str(d_off.room_name).strip() not in ("", "—")
                and d_off.price is not None
                and d_off.price > 0
            ]
            booking_offers = [
                off
                for off in list(getattr(res, "offers", None) or [])
                if getattr(off, "room_name", None)
                and str(off.room_name).strip() not in ("", "—")
                and off.price is not None
                and off.price > 0
            ]

            for d_off in direct_offers:
                direct_room_name = str(d_off.room_name).strip()
                cap_group = d_off.capacity_group if d_off.capacity_group else None
                raw_matched = getattr(d_off, "matched_room_name", None)
                matched_booking_name = (
                    str(raw_matched).strip()
                    if raw_matched and str(raw_matched).strip() not in ("", "—")
                    else None
                )
                matched_room_key = (
                    matched_booking_name
                    if matched_booking_name
                    else f"Direct Only: {direct_room_name}"
                )
                direct_bonuses = (
                    d_off.bonuses
                    if d_off.bonuses is not None
                    else (
                        " | ".join(
                            p
                            for p in (d_off.meal_plan, d_off.cancellation_policy)
                            if p
                        )
                        or None
                    )
                )

                _register_record(
                    {
                        "scraped_date": scraped_date_str,
                        "scraped_at": scraped_at_str,
                        "hotel_id": res.hotel_id,
                        "hotel_name": res.hotel_name,
                        "check_in": check_in_str,
                        "check_out": check_out_str,
                        "nights": nights,
                        "lead_time": lead_time,
                        "adults": adults,
                        "children": children,
                        "children_ages": children_ages_str,
                        "channel": "Direct",
                        "availability_status": "AVAILABLE",
                        "capacity_group": cap_group,
                        "matched_room_key": matched_room_key,
                        "room_name": direct_room_name,
                        "price": round(float(d_off.price), 2),
                        "bonuses": direct_bonuses,
                    }
                )

            if bool(res.direct_url) and not direct_offers and res.direct_price is None:
                direct_not_avail_bonuses = (
                    getattr(res, "direct_error_message", None)
                    or "Termín nie je dostupný v rezervačnom kalendári / vypredané"
                )
                _register_record(
                    {
                        "scraped_date": scraped_date_str,
                        "scraped_at": scraped_at_str,
                        "hotel_id": res.hotel_id,
                        "hotel_name": res.hotel_name,
                        "check_in": check_in_str,
                        "check_out": check_out_str,
                        "nights": nights,
                        "lead_time": lead_time,
                        "adults": adults,
                        "children": children,
                        "children_ages": children_ages_str,
                        "channel": "Direct",
                        "availability_status": "NOT_AVAILABLE",
                        "capacity_group": None,
                        "matched_room_key": "Žiadne voľné ubytovanie",
                        "room_name": "Žiadne voľné ubytovanie",
                        "price": None,
                        "bonuses": direct_not_avail_bonuses,
                    }
                )

            for b_off in booking_offers:
                booking_room_name = str(b_off.room_name).strip()
                cap_group = b_off.capacity_group if b_off.capacity_group else None
                booking_bonuses = (
                    b_off.bonuses
                    if b_off.bonuses is not None
                    else (
                        " | ".join(
                            p
                            for p in (b_off.meal_plan, b_off.cancellation_policy)
                            if p
                        )
                        or None
                    )
                )

                _register_record(
                    {
                        "scraped_date": scraped_date_str,
                        "scraped_at": scraped_at_str,
                        "hotel_id": res.hotel_id,
                        "hotel_name": res.hotel_name,
                        "check_in": check_in_str,
                        "check_out": check_out_str,
                        "nights": nights,
                        "lead_time": lead_time,
                        "adults": adults,
                        "children": children,
                        "children_ages": children_ages_str,
                        "channel": "Booking",
                        "availability_status": "AVAILABLE",
                        "capacity_group": cap_group,
                        "matched_room_key": booking_room_name,
                        "room_name": booking_room_name,
                        "price": round(float(b_off.price), 2),
                        "bonuses": booking_bonuses,
                    }
                )

            if bool(res.booking_url) and not booking_offers and res.booking_price is None:
                booking_not_avail_bonuses = (
                    getattr(res, "booking_error_message", None)
                    or "Vypredané / nedostupné na zadaný termín"
                )
                _register_record(
                    {
                        "scraped_date": scraped_date_str,
                        "scraped_at": scraped_at_str,
                        "hotel_id": res.hotel_id,
                        "hotel_name": res.hotel_name,
                        "check_in": check_in_str,
                        "check_out": check_out_str,
                        "nights": nights,
                        "lead_time": lead_time,
                        "adults": adults,
                        "children": children,
                        "children_ages": children_ages_str,
                        "channel": "Booking",
                        "availability_status": "NOT_AVAILABLE",
                        "capacity_group": None,
                        "matched_room_key": "Žiadne voľné ubytovanie",
                        "room_name": "Žiadne voľné ubytovanie",
                        "price": None,
                        "bonuses": booking_not_avail_bonuses,
                    }
                )

        return list(deduped.values())

    def sync_room_catalog(self, hotel_identifiers: Optional[List[str]] = None) -> int:
        """Synchronize date-independent room catalogs and persist mappings to hs-room_mappings."""
        logger.info("Starting date-independent room catalog synchronization (--sync-rooms)...")

        try:
            target_hotels = self.config_manager.get_target_hotels()
        except ConfigError as exc:
            logger.error(f"Cannot synchronize room catalogs without target hotels: {exc}")
            return 0

        filter_ids = hotel_identifiers if hotel_identifiers is not None else self.job.hotel_identifiers
        if filter_ids:
            target_hotels = [
                h
                for h in target_hotels
                if h.get("id") in filter_ids or h.get("name") in filter_ids
            ]

        total = len(target_hotels)
        if total == 0:
            logger.warning("No target hotels matched for room catalog synchronization.")
            return 0

        booking_rules = self._get_rules("booking")
        global_matching_rules = self._get_rules("room_matching")
        total_synced = 0

        try:
            self.browser_manager.launch_browser()

            for index, hotel in enumerate(target_hotels, start=1):
                if self.stop_event is not None and self.stop_event.is_set():
                    logger.info("Room catalog sync aborted due to stop_event signal.")
                    break

                hotel_id = str(hotel.get("id", ""))
                hotel_name = str(hotel.get("name", f"Hotel #{index}"))
                catalog_booking_url = str(
                    hotel.get("booking_rooms_url") or hotel.get("booking_url") or ""
                )
                catalog_direct_url = str(
                    hotel.get("rooms_catalog_url") or hotel.get("direct_url") or ""
                )
                direct_engine = str(hotel.get("direct_booking_engine") or "generic")

                logger.info(f"[{index}/{total}] Syncing room catalog for: {hotel_name}")

                page = self.browser_manager.new_page()
                try:
                    booking_offers: List[RoomOffer] = []
                    direct_offers: List[RoomOffer] = []

                    if catalog_booking_url:
                        try:
                            catalog_booking_url = self.booking_parser.build_catalog_booking_url(
                                catalog_booking_url
                            )
                            logger.debug(f"Navigating to undated Booking catalog: {catalog_booking_url}")
                            page.goto(catalog_booking_url, wait_until="domcontentloaded")
                            booking_offers = self.booking_parser.parse_room_catalog(
                                page=page,
                                rules=booking_rules,
                                hotel_name=hotel_name,
                            )
                        except Exception as exc:
                            logger.error(f"Error syncing Booking room catalog for {hotel_name}: {exc}")

                    if catalog_direct_url:
                        try:
                            direct_rules = self._get_rules(direct_engine)
                            logger.debug(f"Navigating to static direct catalog: {catalog_direct_url}")
                            page.goto(catalog_direct_url, wait_until="domcontentloaded")
                            direct_offers = self.direct_parser.parse_room_catalog(
                                page=page,
                                rules=direct_rules,
                                hotel_name=hotel_name,
                            )
                        except Exception as exc:
                            logger.error(f"Error syncing direct room catalog for {hotel_name}: {exc}")

                    existing_mappings: List[Dict[str, Any]] = []
                    try:
                        fetched = self.config_manager.get_room_mappings(hotel_id)
                        if isinstance(fetched, list):
                            existing_mappings = fetched
                    except Exception as exc:
                        logger.warning(f"Could not fetch existing mappings for {hotel_name}: {exc}")

                    mappings = self.room_matcher.build_catalog_mappings(
                        hotel=hotel,
                        booking_offers=booking_offers,
                        direct_offers=direct_offers,
                        global_rules=global_matching_rules,
                        existing_mappings=existing_mappings,
                    )

                    if mappings:
                        if self.config_manager.upsert_room_mappings(mappings):
                            total_synced += len(mappings)
                            logger.info(
                                f"Synchronizovaných {len(mappings)} izieb pre hotel '{hotel_name}'."
                            )
                        else:
                            logger.warning(
                                f"Nepodarilo sa uložiť mapovania izieb pre hotel '{hotel_name}'."
                            )
                    else:
                        logger.warning(f"Pre hotel '{hotel_name}' neboli vyťažené žiadne izby na mapovanie.")
                except Exception as exc:
                    logger.error(f"Unexpected error during room sync for {hotel_name}: {exc}")
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass

                if index < total and self.max_jitter > 0:
                    pause = random.uniform(self.min_jitter, self.max_jitter)
                    logger.debug(f"Applying stealth pause of {pause:.2f}s...")
                    time.sleep(pause)

            logger.info(
                f"Room catalog synchronization finished. Total synced mappings: {total_synced}."
            )
        finally:
            self.browser_manager.close()

        return total_synced

    def run(
        self, output_path: Optional[Union[str, Path]] = None
    ) -> List[ScrapedResult]:
        """Execute the end-to-end price comparison scraping job.

        Returns:
            List of ScrapedResult objects containing paired prices and deltas.
        """
        logger.info(
            f"Starting ScrapingEngine for {self.job.nights} nights "
            f"({self.job.check_in} -> {self.job.check_out}) for {self.job.total_guests} guests."
        )

        # 1. Fetch target hotels
        try:
            target_hotels = self.config_manager.get_target_hotels()
        except ConfigError as exc:
            logger.error(f"Cannot run scraper without target hotels: {exc}")
            self._send_progress({"status": "failed", "error": str(exc)})
            return []

        # Filter hotels if requested in job
        if self.job.hotel_identifiers:
            target_hotels = [
                h
                for h in target_hotels
                if h.get("id") in self.job.hotel_identifiers
                or h.get("name") in self.job.hotel_identifiers
            ]

        total = len(target_hotels)
        if total == 0:
            logger.warning("No target hotels to scrape matching the criteria.")
            self._send_progress({"status": "completed", "results_count": 0})
            return []

        # 2. Pre-fetch rules for Booking
        booking_rules = self._get_rules("booking")

        results: List[ScrapedResult] = []

        # 3. Launch browser and run loop
        self._in_run_loop = True
        try:
            self.browser_manager.launch_browser()

            for index, hotel in enumerate(target_hotels, start=1):
                if self.stop_event is not None and self.stop_event.is_set():
                    logger.info("Scraping loop aborted due to stop_event signal.")
                    break

                hotel_id = hotel.get("id", "")
                hotel_name = hotel.get("name", f"Hotel #{index}")
                pct = round((index / total) * 100, 1)

                # Send granular progress update pre UI
                self._send_progress(
                    {
                        "status": "running",
                        "current": index,
                        "total": total,
                        "hotel": hotel_name,
                        "percent": pct,
                    }
                )

                logger.info(f"[{index}/{total}] Processing hotel: {hotel_name}")

                # Čistá stránka (tab) pre každý hotel na izoláciu cookies a stavov
                page = self.browser_manager.new_page()
                try:
                    result = self._scrape_single_hotel(hotel, page, booking_rules)
                    results.append(result)
                except Exception as exc:
                    logger.error(f"Unexpected crash processing {hotel_name}: {exc}")
                    results.append(
                        ScrapedResult(
                            hotel_id=hotel_id,
                            hotel_name=hotel_name,
                            check_in=self.job.check_in,
                            check_out=self.job.check_out,
                            booking_url=hotel.get("booking_url") or None,
                            direct_url=hotel.get("direct_url") or None,
                            status=ResultStatus.FAILED,
                            is_available=False,
                            error_message=f"Fatal hotel extraction error: {exc}",
                            booking_error_message=f"Fatal hotel extraction error: {exc}",
                            direct_error_message=f"Fatal hotel extraction error: {exc}",
                        )
                    )
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass

                # Stealth pauza medzi hotelmi
                if index < total and self.max_jitter > 0:
                    pause = random.uniform(self.min_jitter, self.max_jitter)
                    logger.debug(f"Applying stealth pause of {pause:.2f}s...")
                    time.sleep(pause)

            # 4. Persist collected price comparisons into Supabase hs-price_comparisons
            try:
                comparison_records = self.build_price_comparison_records(results)
                if comparison_records:
                    upserted_count = self.config_manager.upsert_price_comparisons(
                        comparison_records
                    )
                    logger.info(
                        f"Uložených/aktualizovaných {upserted_count} porovnaní cien v hs-price_comparisons."
                    )
            except Exception as exc:
                logger.warning(
                    f"Nepodarilo sa uložiť porovnania cien do hs-price_comparisons: {exc}"
                )

            effective_output = output_path or self.output_path
            if effective_output is not None:
                try:
                    db_rows = self.config_manager.fetch_all_price_comparisons()
                    rows_list = db_rows if isinstance(db_rows, list) else []
                    generated_path = ExcelReportGenerator().generate_raw_db_report(
                        rows=rows_list,
                        output_path=effective_output,
                    )
                    self.last_report_path = str(generated_path)
                except Exception as exc:
                    logger.warning(
                        f"Nepodarilo sa vygenerovať surový DB Excel report v ScrapingEngine.run: {exc}"
                    )

            # 5. Final completion event (až po ukončení celej slučky)
            self._send_progress(
                {
                    "status": "completed",
                    "results_count": len(results),
                    "total": total,
                    "current": total,
                    "percent": 100.0,
                }
            )
            logger.info(f"Scraping job finished. Successfully processed {len(results)} hotels.")

        finally:
            self._in_run_loop = False
            # 6. Always close browser cleanly
            self.browser_manager.close()

        return results
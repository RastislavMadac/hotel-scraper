"""Dynamic Booking.com parser adhering to Zero-Update architecture and defensive parsing."""

from datetime import datetime
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.sync_api import Locator, Page

from config.settings import get_settings
from core.logger import logger
from core.models import ResultStatus, RoomOffer, ScrapedResult, ScrapingJob

SCREENSHOTS_DIR = get_settings().logs_dir / "screenshots"


def clean_price_text(raw_text: str, price_regex: Optional[str] = None) -> Optional[float]:
    """Extract and normalize a numeric price value from raw scraped text."""
    if not raw_text or not isinstance(raw_text, str):
        return None

    cleaned = raw_text.replace("\xa0", " ").replace("\u202f", " ").strip()

    if price_regex:
        match = re.search(price_regex, cleaned)
        if match:
            cleaned = match.group(1) if match.groups() else match.group(0)

    cleaned = re.sub(r"[^\d.,]", "", cleaned).strip()
    if not cleaned:
        return None

    try:
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            parts = cleaned.split(",")
            if len(parts) == 2 and len(parts[1]) in (1, 2):
                cleaned = cleaned.replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "." in cleaned:
            parts = cleaned.split(".")
            if len(parts) == 2 and len(parts[1]) in (1, 2):
                pass
            elif len(parts) == 2 and len(parts[1]) == 3:
                cleaned = cleaned.replace(".", "")

        return round(float(cleaned), 2)
    except (ValueError, TypeError) as exc:
        logger.debug(f"Failed to parse cleaned price '{cleaned}': {exc}")
        return None


class BookingParser:
    """Dynamic extractor for Booking.com using remote rules and defensive parsing."""

    def __init__(self, screenshots_dir: Optional[Path] = None) -> None:
        self.screenshots_dir = Path(screenshots_dir or SCREENSHOTS_DIR)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    def build_booking_url(self, base_url: str, job: ScrapingJob) -> str:
        """Append check-in, check-out, and guest count parameters to Booking.com URL."""
        if not base_url:
            return ""
        parsed = urlparse(base_url)
        overridden_keys = {
            "checkin",
            "checkout",
            "group_adults",
            "group_children",
            "no_rooms",
            "age",
        }
        params: List[Tuple[str, str]] = [
            (k, v)
            for k, v in parse_qsl(parsed.query)
            if k.lower() not in overridden_keys
        ]
        params.extend(
            [
                ("checkin", job.check_in.strftime("%Y-%m-%d")),
                ("checkout", job.check_out.strftime("%Y-%m-%d")),
                ("group_adults", str(job.adults)),
                ("group_children", str(job.children)),
                ("no_rooms", "1"),
            ]
        )
        if job.children > 0 and job.children_ages:
            for age in job.children_ages:
                params.append(("age", str(age)))
        new_query = urlencode(params)
        return urlunparse(parsed._replace(query=new_query))

    def build_catalog_booking_url(self, base_url: str) -> str:
        """Return base Booking.com URL stripped of date and guest parameters for catalog sync."""
        if not base_url:
            return ""
        parsed = urlparse(base_url)
        excluded = {
            "checkin",
            "checkout",
            "group_adults",
            "group_children",
            "no_rooms",
            "age",
        }
        filtered_params = [
            (k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in excluded
        ]
        new_query = urlencode(filtered_params)
        return urlunparse(parsed._replace(query=new_query))

    def _save_diagnostic_screenshot(self, page: Page, hotel_name: str) -> Optional[Path]:
        """Safely capture a screenshot for debugging unidentified layouts."""
        try:
            safe_slug = re.sub(r"[^\w]+", "_", hotel_name.strip().lower()).strip("_")
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"booking_{safe_slug}_{timestamp}.png"
            target_path = self.screenshots_dir / filename
            page.screenshot(path=str(target_path), full_page=False)
            logger.info(f"Saved diagnostic screenshot to: {target_path}")
            return target_path
        except Exception as exc:
            logger.warning(f"Could not save diagnostic screenshot for {hotel_name}: {exc}")
            return None

    def _dismiss_cookie_banners(self, page: Page, rules: Dict[str, Any]) -> None:
        """Dynamically dismiss cookie consent modals based on remote rules."""
        consent_selectors: List[str] = rules.get(
            "cookie_consent_selectors",
            [
                "#onetrust-accept-btn-handler",
                "button#onetrust-accept-btn-handler",
                "button[id*='cookie'][id*='accept']",
                "button:has-text('Prijať všetky')",
                "button:has-text('Súhlasím')",
                "button:has-text('Accept all')",
            ],
        )
        for selector in consent_selectors:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=1500):
                    btn.click(timeout=1500)
                    logger.debug(f"Dismissed cookie consent via selector: {selector}")
                    page.wait_for_timeout(500)
                    break
            except Exception:
                continue

    @staticmethod
    def _parse_occupancy_text(text: str) -> Optional[int]:
        """Parse adult and child counts from occupancy text and return their sum."""
        if not text:
            return None
        normalized = " ".join(text.split())
        adult_match = re.search(
            r"\b(?:dospel\w*|adults?)\.?\s*:?\s*(\d+)", normalized, re.IGNORECASE
        ) or re.search(
            r"(\d+)\s+(?:dospel\w*|adults?)\b", normalized, re.IGNORECASE
        )
        child_match = re.search(
            r"\b(?:deti|detí|det\.?|deť\w*|děti|dětí|dieťa\w*|children|child)\s*:?\s*(\d+)",
            normalized,
            re.IGNORECASE,
        ) or re.search(
            r"(\d+)\s+(?:deti|detí|det\.?|deť\w*|děti|dětí|dieťa\w*|children|child)\b",
            normalized,
            re.IGNORECASE,
        )
        if adult_match or child_match:
            adults_count = int(adult_match.group(1)) if adult_match else 0
            children_count = int(child_match.group(1)) if child_match else 0
            total = adults_count + children_count
            if total > 0:
                return total
        return None

    def _extract_occupancy(
        self,
        row: Locator,
        occupancy_selectors: List[str],
        icon_selectors: List[str],
    ) -> Optional[int]:
        """Extract total number of guests (adults + children) from a room table row."""
        for occ_sel in occupancy_selectors:
            try:
                occ_cell = row.locator(occ_sel).first
                if occ_cell.count() == 0:
                    continue

                # 1. Kontrola screen-reader / textového popisu (dospelí + deti)
                sr_loc = occ_cell.locator("span.bui-u-sr-only, .bui-u-sr-only")
                sr_count = sr_loc.count()
                if sr_count > 0:
                    sr_texts = []
                    for i in range(sr_count):
                        part = sr_loc.nth(i).text_content()
                        if part and part.strip():
                            sr_texts.append(part.strip())
                    if sr_texts:
                        parsed_total = self._parse_occupancy_text(" ".join(sr_texts))
                        if parsed_total is not None:
                            return parsed_total

                cell_text = occ_cell.text_content()
                if cell_text:
                    parsed_total = self._parse_occupancy_text(cell_text)
                    if parsed_total is not None:
                        return parsed_total

                # 2. Kontrola kombinovaného počtu ikon osôb (dospelí + deti)
                combined_parts: List[str] = [
                    "i.bicon-occupancy",
                    "i.bicon-occupancychild",
                    "span.bicon-occupancy",
                    "span.bicon-occupancychild",
                    ".bicon-occupancy",
                    ".bicon-occupancychild",
                ]
                if icon_selectors and icon_selectors[0] and icon_selectors[0] not in combined_parts:
                    combined_parts.insert(0, icon_selectors[0])
                combined_selector = ", ".join(combined_parts)
                combined_count = occ_cell.locator(combined_selector).count()
                if combined_count > 0:
                    return combined_count

                for icon_sel in icon_selectors:
                    icon_locator = occ_cell.locator(icon_sel)
                    icon_count = icon_locator.count()
                    if icon_count > 0:
                        return icon_count

                # 3. Kontrola aria-label alebo title atribútov
                for attr_name in ("aria-label", "title", "data-title"):
                    attr_val = occ_cell.get_attribute(attr_name)
                    if attr_val:
                        parsed_attr = self._parse_occupancy_text(attr_val)
                        if parsed_attr is not None:
                            return parsed_attr
                        m = re.search(r"(\d+)", attr_val)
                        if m and int(m.group(1)) > 0:
                            return int(m.group(1))

                # 4. Všeobecný číselný fallback z textového obsahu bunky
                if cell_text:
                    m = re.search(r"(\d+)", cell_text)
                    if m and int(m.group(1)) > 0:
                        return int(m.group(1))

            except Exception:
                continue

        return None

    def _extract_bed_info(self, row: Locator, bed_selectors: List[str]) -> Optional[str]:
        """Extract and join unique bed configuration texts from a room row."""
        for b_sel in bed_selectors:
            try:
                loc = row.locator(b_sel)
                count = loc.count()
                if count == 0:
                    continue
                parts: List[str] = []
                seen = set()
                for i in range(count):
                    raw_text = loc.nth(i).text_content()
                    if raw_text:
                        cleaned = " ".join(raw_text.split())
                        if cleaned and cleaned.lower() not in seen:
                            seen.add(cleaned.lower())
                            parts.append(cleaned)
                if parts:
                    return " + ".join(parts)
            except Exception:
                continue
        return None

    def _extract_bed_info_from_name_element(
        self,
        name_loc: Locator,
        bed_selectors: List[str],
        fallback_row: Optional[Locator] = None,
    ) -> Optional[str]:
        """Extract bed info from the nearest enclosing row/container of a room name element."""
        ancestor_xpaths = [
            "xpath=ancestor::tr[1]",
            "xpath=ancestor::div[.//span[contains(@class,'ddaf087582')] or .//*[@data-testid='bed-info']][1]",
            "xpath=ancestor::div[.//*[contains(@class,'ddaf087582')] or .//*[@data-testid='bed-info'] or .//*[contains(@class,'rt-bed-types')]][1]",
            "xpath=ancestor::*[self::tr or @data-testid='property-card' or contains(@class,'room-row')][1]",
        ]
        for anc_xpath in ancestor_xpaths:
            try:
                anc_loc = name_loc.locator(anc_xpath)
                if anc_loc.count() > 0:
                    bed_info = self._extract_bed_info(anc_loc, bed_selectors)
                    if not bed_info:
                        bed_info = self._extract_bed_info(anc_loc.first, bed_selectors)
                    if bed_info:
                        return bed_info
            except Exception:
                continue

        if fallback_row is not None:
            return self._extract_bed_info(fallback_row, bed_selectors)
        return None

    def parse_room_catalog(
        self,
        page: Page,
        rules: Dict[str, Any],
        hotel_name: str,
    ) -> List[RoomOffer]:
        """Parse undated Booking.com page to extract all available room types and bed info."""
        try:
            rules = rules or {}
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception as exc:
                logger.debug(f"domcontentloaded timeout for catalog {hotel_name}: {exc}")

            self._dismiss_cookie_banners(page, rules)

            default_room_name_selectors = [
                ".b08850ce41",
                ".hprt-roomtype-icon-link",
                ".hprt-roomtype-link",
                "[data-testid='room-name']",
                ".room_link span",
            ]
            raw_room_name_selectors: List[str] = rules.get(
                "room_name_selectors", default_room_name_selectors
            )
            room_name_selectors = list(
                dict.fromkeys([*raw_room_name_selectors, *default_room_name_selectors])
            )

            default_bed_selectors = [
                ".ddaf087582",
                "[data-testid='bed-info']",
                ".rt-bed-types",
            ]
            raw_bed_selectors: List[str] = rules.get(
                "bed_selectors", default_bed_selectors
            )
            bed_selectors = list(
                dict.fromkeys([*raw_bed_selectors, *default_bed_selectors])
            )

            wait_candidates = ", ".join(
                dict.fromkeys(
                    [
                        "table.hprt-table",
                        "[data-testid='property-section--rooms-table']",
                        *room_name_selectors,
                    ]
                )
            )
            try:
                page.wait_for_selector(wait_candidates, timeout=8000)
            except Exception:
                page.wait_for_timeout(1000)

            offers: List[RoomOffer] = []
            seen_pairs = set()

            rows = page.locator("tr, [data-testid='property-card'], li")
            row_count = rows.count()
            for idx in range(row_count):
                try:
                    row = rows.nth(idx)
                    room_name: Optional[str] = None
                    for n_sel in room_name_selectors:
                        try:
                            name_loc = row.locator(n_sel).first
                            if name_loc.count() > 0:
                                val = name_loc.text_content()
                                if val and val.strip():
                                    room_name = " ".join(val.split())
                                    break
                        except Exception:
                            continue

                    if not room_name:
                        continue

                    bed_info = self._extract_bed_info(row, bed_selectors)
                    if not bed_info:
                        continue

                    dedup_key = (room_name.lower(), bed_info.lower())
                    if dedup_key in seen_pairs:
                        continue
                    seen_pairs.add(dedup_key)

                    offers.append(
                        RoomOffer(
                            room_name=room_name,
                            price=0.0,
                            bed_info=bed_info,
                        )
                    )
                except Exception as row_exc:
                    logger.debug(
                        f"Chyba pri spracovaní katalógového riadku {idx} pre {hotel_name}: {row_exc}"
                    )
                    continue

            logger.info(
                f"Katalóg Booking pre '{hotel_name}': nájdených {len(offers)} unikátnych izieb."
            )
            return offers
        except Exception as exc:
            logger.error(f"Chyba pri parsovaní Booking katalógu pre {hotel_name}: {exc}")
            return []

    def parse_hotel_page(
        self,
        page: Page,
        rules: Dict[str, Any],
        hotel_name: str,
        requested_guests: int = 2,
        job: Optional[ScrapingJob] = None,
    ) -> ScrapedResult:
        """Parse hotel details extracting ALL room offers matching requested occupancy."""
        target_guests = (
            job.total_guests
            if job is not None
            else (
                requested_guests.total_guests
                if isinstance(requested_guests, ScrapingJob)
                else int(requested_guests)
            )
        )
        try:
            # 0. Čakanie na DOM
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception as exc:
                logger.debug(f"domcontentloaded timeout for {hotel_name}: {exc}")

            # 1. Odkliknutie cookies
            self._dismiss_cookie_banners(page, rules)

            # 2. Čakanie na tabuľku alebo banner vypredania
            try:
                page.wait_for_selector(
                    "table.hprt-table, [data-testid='property-section--rooms-table'], .fe_banner__message, [data-testid='sold-out-property-banner']",
                    timeout=12000,
                )
            except Exception:
                page.wait_for_timeout(1500)

            # 3. Kontrola stavu vypredania
            sold_out_selectors: List[str] = rules.get("sold_out_selectors", [])
            for selector in sold_out_selectors:
                try:
                    locator = page.locator(selector)
                    count = locator.count()
                    if count > 0 and any(locator.nth(i).is_visible() for i in range(count)):
                        logger.info(f"Hotel {hotel_name} je na Bookingu vypredaný.")
                        return ScrapedResult(
                            hotel_name=hotel_name,
                            is_available=False,
                            booking_price=None,
                            offers=[],
                            error_message=None,
                        )
                except Exception as exc:
                    logger.debug(f"Error checking sold_out_selector '{selector}': {exc}")

            # 4. Načítanie remote selektorov
            room_row_selectors: List[str] = rules.get(
                "room_row_selectors",
                [
                    "table.hprt-table tbody tr",
                    "[data-testid='property-section--rooms-table'] tbody tr",
                    "tr[data-block-id]",
                ],
            )
            occupancy_selectors: List[str] = rules.get(
                "occupancy_selectors",
                ["td.hprt-table-cell-occupancy", ".hprt-occupancy-occupancy-info", ".c-occupancy"],
            )
            occupancy_icon_selectors: List[str] = rules.get(
                "occupancy_icon_selectors",
                [
                    ".bicon-occupancy",
                    ".bicon-occupancychild",
                    "i.bicon-occupancy",
                    "i.bicon-occupancychild",
                    "span.bicon-occupancy",
                    "span.bicon-occupancychild",
                    "svg",
                ],
            )
            room_name_selectors: List[str] = rules.get(
                "room_name_selectors",
                [
                    ".hprt-roomtype-icon-link",
                    ".hprt-roomtype-link",
                    "[data-testid='room-name']",
                    ".room_link span",
                    ".b08850ce41",
                ],
            )
            bed_selectors: List[str] = rules.get(
                "bed_selectors",
                [".ddaf087582", "[data-testid='bed-info']", ".rt-bed-types"],
            )
            price_selectors: List[str] = rules.get(
                "price_selectors",
                [
                    ".bui-price-display__value",
                    "[data-testid='price-and-discounted-final-price']",
                    ".prco-valign-middle-helper",
                    ".hprt-price-price",
                ],
            )
            meal_plan_selectors: List[str] = rules.get(
                "meal_plan_selectors",
                [".hprt-condition-detail", ".hprt-table-cell-conditions", ".bui-list__item"],
            )
            cancellation_selectors: List[str] = rules.get(
                "cancellation_selectors",
                [".hprt-cancellation-policy", ".hprt-table-cell-conditions"],
            )
            price_regex: Optional[str] = rules.get("price_regex")

            matched_offers: List[RoomOffer] = []
            rows_detected_on_page = False
            discarded_nameless_price = False

            # 5. Vyhľadanie riadkov cez fallback selektory
            for row_selector in room_row_selectors:
                try:
                    rows = page.locator(row_selector)
                    row_count = rows.count()
                    if row_count == 0:
                        continue

                    rows_detected_on_page = True
                    logger.debug(f"Pre {hotel_name} nájdených {row_count} riadkov cez selector '{row_selector}'")
                    current_room_name: Optional[str] = None
                    last_known_occupancy = target_guests
                    last_known_bed_info: Optional[str] = None

                    for idx in range(row_count):
                        try:
                            row = rows.nth(idx)

                            # A) Detekcia zmeny názvu izby (nový blok izby)
                            has_name_el = False
                            found_valid_name: Optional[str] = None
                            for n_sel in room_name_selectors:
                                name_loc = row.locator(n_sel).first
                                if name_loc.count() > 0:
                                    has_name_el = True
                                    val = name_loc.text_content()
                                    if val and val.strip():
                                        cleaned_val = " ".join(val.split())
                                        if cleaned_val not in ("", "—"):
                                            found_valid_name = cleaned_val
                                            break
                            if has_name_el:
                                current_room_name = found_valid_name
                                last_known_occupancy = target_guests
                                last_known_bed_info = None

                            # A2) Extrakcia postelí s dedením pre cenové sub-riadky
                            extracted_beds = self._extract_bed_info(row, bed_selectors)
                            if extracted_beds is not None:
                                last_known_bed_info = extracted_beds

                            # B) Detekcia kapacity aktuálneho riadku
                            extracted_occ = self._extract_occupancy(
                                row, occupancy_selectors, occupancy_icon_selectors
                            )
                            if extracted_occ is not None:
                                last_known_occupancy = extracted_occ

                            # Ak riadok explicitne patrí inej kapacite než požadovanej, preskakujeme
                            if last_known_occupancy != target_guests:
                                continue

                            # C) Extrakcia ceny
                            price: Optional[float] = None
                            for p_sel in price_selectors:
                                p_loc = row.locator(p_sel).first
                                if p_loc.count() > 0 and p_loc.is_visible():
                                    raw_p = p_loc.text_content()
                                    if raw_p:
                                        p_val = clean_price_text(raw_p, price_regex=price_regex)
                                        if p_val is not None and p_val > 0:
                                            price = p_val
                                            break

                            if price is None:
                                continue

                            if not current_room_name or current_room_name.strip() in ("", "—"):
                                discarded_nameless_price = True
                                continue

                            # D) Strava a storno podmienky
                            meal_plan: Optional[str] = None
                            for m_sel in meal_plan_selectors:
                                m_loc = row.locator(m_sel).first
                                if m_loc.count() > 0:
                                    raw_m = m_loc.text_content()
                                    if raw_m and raw_m.strip():
                                        meal_plan = " ".join(raw_m.split())
                                        break

                            cancellation: Optional[str] = None
                            for c_sel in cancellation_selectors:
                                c_loc = row.locator(c_sel).first
                                if c_loc.count() > 0:
                                    practical_c = c_loc.text_content()
                                    if practical_c and practical_c.strip():
                                        cancellation = " ".join(practical_c.split())
                                        break

                            matched_offers.append(
                                RoomOffer(
                                    room_name=current_room_name,
                                    occupancy=target_guests,
                                    price=price,
                                    currency="EUR",
                                    meal_plan=meal_plan,
                                    cancellation_policy=cancellation,
                                    bed_info=last_known_bed_info,
                                )
                            )

                        except Exception as row_exc:
                            logger.debug(f"Chyba pri spracovaní riadku {idx} pre {hotel_name}: {row_exc}")
                            continue

                    if matched_offers:
                        break

                except Exception as sel_exc:
                    logger.debug(f"Chyba pri selektore riadkov '{row_selector}': {sel_exc}")
                    continue

            valid_offers = [
                offer
                for offer in matched_offers
                if getattr(offer, "room_name", None)
                and offer.room_name.strip() not in ("", "—")
            ]
            if len(valid_offers) < len(matched_offers):
                discarded_nameless_price = True
            matched_offers = valid_offers

            if matched_offers:
                lowest_price = min(o.price for o in matched_offers)
                logger.info(
                    f"Úspešne vyťažených {len(matched_offers)} ponúk pre {hotel_name} "
                    f"(počet hostí: {target_guests}, najnižšia cena: {lowest_price:.2f} €)"
                )
                return ScrapedResult(
                    hotel_name=hotel_name,
                    is_available=True,
                    booking_price=lowest_price,
                    offers=matched_offers,
                    error_message=None,
                )

            # Fallback pre zobrazenie bez tabuľky riadkov (iba ak existuje konkrétny názov izby)
            if not rows_detected_on_page:
                fallback_name_selectors = list(room_name_selectors)
                if "room_name_selectors" not in rules:
                    fallback_name_selectors.append(".hotel-card h2")
                fallback_room_name: Optional[str] = None
                for n_sel in fallback_name_selectors:
                    try:
                        n_loc = page.locator(n_sel).first
                        if n_loc.count() > 0:
                            raw_n = n_loc.text_content()
                            if raw_n:
                                cleaned_n = " ".join(raw_n.split())
                                if cleaned_n and cleaned_n not in ("", "—"):
                                    fallback_room_name = cleaned_n
                                    break
                    except Exception:
                        continue

                for selector in price_selectors:
                    try:
                        locator = page.locator(selector).first
                        if locator.count() > 0 and locator.is_visible():
                            raw_text = locator.text_content()
                            if raw_text:
                                price = clean_price_text(raw_text, price_regex=price_regex)
                                if price is not None and price > 0:
                                    if not fallback_room_name:
                                        discarded_nameless_price = True
                                        continue
                                    logger.info(
                                        f"Nájdená cena pre {hotel_name} cez fallback selektor '{selector}': {price:.2f} €"
                                    )
                                    single_offer = RoomOffer(
                                        room_name=fallback_room_name,
                                        occupancy=target_guests,
                                        price=price,
                                        currency="EUR",
                                        meal_plan=None,
                                        cancellation_policy=None,
                                    )
                                    return ScrapedResult(
                                        hotel_name=hotel_name,
                                        is_available=True,
                                        booking_price=price,
                                        offers=[single_offer],
                                        error_message=None,
                                    )
                    except Exception as exc:
                        logger.debug(f"Chyba pri fallback price_selector '{selector}': {exc}")

            if discarded_nameless_price:
                logger.info(
                    f"Pre {hotel_name} bola zachytená cena bez rozpoznaného názvu izby na Bookingu -> NOT_AVAILABLE."
                )
                return ScrapedResult(
                    hotel_name=hotel_name,
                    is_available=False,
                    status=ResultStatus.NOT_AVAILABLE,
                    booking_price=None,
                    offers=[],
                    error_message=None,
                )

            # Diagnostický screenshot ak cena ani nedostupnosť neboli nájdené
            self._save_diagnostic_screenshot(page, hotel_name)
            err_msg = f"Cena ani stav nedostupnosti neboli identifikované (počet hostí: {target_guests})"
            logger.warning(f"Pre hotel '{hotel_name}': {err_msg}")
            return ScrapedResult(
                hotel_name=hotel_name,
                is_available=False,
                booking_price=None,
                offers=[],
                error_message=err_msg,
            )

        except Exception as exc:
            logger.error(f"Kritická chyba pri parsovaní Booking stránky pre {hotel_name}: {exc}")
            self._save_diagnostic_screenshot(page, hotel_name)
            return ScrapedResult(
                hotel_name=hotel_name,
                is_available=False,
                booking_price=None,
                offers=[],
                error_message=f"Chyba parsovania: {exc}",
            )

"""Dynamic direct hotel booking engine parser adhering to Zero-Update architecture."""

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.sync_api import Locator, Page

from config.settings import get_settings
from core.logger import logger
from core.models import ResultStatus, RoomOffer, ScrapedResult, ScrapingJob
from scraper.booking_parser import clean_price_text

SCREENSHOTS_DIR = get_settings().logs_dir / "screenshots"
UNAVAILABLE_DATE_MESSAGE = "Termín nie je dostupný v rezervačnom kalendári / vypredané"
DISABLED_CALENDAR_TOKENS = (
    "disabled",
    "unavailable",
    "sold-out",
    "soldout",
    "not-available",
    "booked",
    "inactive",
)
DEFAULT_OFFER_ITEM_SELECTORS = (
    ".onerate",
    "div[data-offer-wrapper]",
)


class DirectParser:
    """Dynamic parser for direct hotel booking systems (Token-based POST, Profitroom, custom)."""

    def __init__(self, screenshots_dir: Optional[Path] = None) -> None:
        self.screenshots_dir = Path(screenshots_dir or SCREENSHOTS_DIR)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    def _save_diagnostic_screenshot(self, page: Page, hotel_name: str) -> Optional[Path]:
        """Safely capture a screenshot for debugging unidentified layouts."""
        try:
            safe_slug = re.sub(r"[^\w]+", "_", hotel_name.strip().lower()).strip("_")
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"direct_{safe_slug}_{timestamp}.png"
            target_path = self.screenshots_dir / filename
            page.screenshot(path=str(target_path), full_page=False)
            logger.info(f"Saved direct booking diagnostic screenshot to: {target_path}")
            return target_path
        except Exception as exc:
            logger.warning(f"Could not save diagnostic screenshot for {hotel_name}: {exc}")
            return None

    def build_direct_url(
        self,
        base_url: str,
        job: ScrapingJob,
        rules: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Construct initial landing URL based on engine method."""
        if not base_url:
            return ""

        raw_rules = rules if isinstance(rules, dict) else {}
        effective_rules = (
            raw_rules.get("rules") if isinstance(raw_rules.get("rules"), dict) else raw_rules
        ) or {}
        method = effective_rules.get("method")

        if method in ("token_api_post", "post_form"):
            return base_url

        date_format = effective_rules.get("date_format", "%Y-%m-%d")
        check_in_str = job.check_in.strftime(date_format)
        check_out_str = job.check_out.strftime(date_format)

        url_pattern: Optional[str] = effective_rules.get("url_pattern") or effective_rules.get("url_template")
        if url_pattern:
            return url_pattern.format(
                base_url=base_url.rstrip("/"),
                check_in=check_in_str,
                check_out=check_out_str,
                adults=job.adults,
                children=job.children,
                total_guests=job.total_guests,
            )

        url_params_mapping: Optional[Dict[str, str]] = effective_rules.get("url_params")
        if url_params_mapping:
            parsed = urlparse(base_url)
            current_params = dict(parse_qsl(parsed.query))
            mapping_replacements = {
                "{check_in}": check_in_str,
                "{check_out}": check_out_str,
                "{adults}": str(job.adults),
                "{children}": str(job.children),
                "{total_guests}": str(job.total_guests),
            }
            for param_key, param_value in url_params_mapping.items():
                val_str = str(param_value)
                for placeholder, actual in mapping_replacements.items():
                    val_str = val_str.replace(placeholder, actual)
                current_params[param_key] = val_str

            return urlunparse(parsed._replace(query=urlencode(current_params)))

        return base_url

    def _build_direct_url(
        self,
        base_url: str,
        job: ScrapingJob,
        rules: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Alias for build_direct_url."""
        return self.build_direct_url(base_url, job, rules)

    def _dismiss_cookie_banners(self, page: Page, rules: Dict[str, Any]) -> None:
        """Dynamically dismiss cookie consent modals based on remote rules."""
        selectors: List[str] = rules.get(
            "cookie_consent_selectors",
            [
                "#onetrust-accept-btn-handler",
                "button[id*='cookie'][id*='accept']",
                "button:has-text('Prijať všetky')",
                "button:has-text('Súhlasím')",
                ".cookie-bar button",
            ],
        )
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible(timeout=1500):
                    locator.click(timeout=1500)
                    logger.debug(f"Direct cookie banner dismissed via selector: {selector}")
                    page.wait_for_timeout(500)
                    break
            except Exception:
                continue

    def _handle_token_api_post(self, page: Page, rules: Dict[str, Any], job: ScrapingJob) -> bool:
        """Fetch search token from API endpoint and navigate directly to search results."""
        token_endpoint = rules.get("token_endpoint", "https://booking.hotelpermon.sk/sk/home/createSearchToken")
        search_template = rules.get("search_url_template", "https://booking.hotelpermon.sk/sk/search/{token}")
        raw_payload = rules.get("form_payload", {})
        date_format = rules.get("date_format", "%Y-%m-%d")
        child_age = str(job.children_ages[0]) if (hasattr(job, "children_ages") and job.children_ages) else "6"

        replacements = {
            "{check_in}": job.check_in.strftime(date_format),
            "{check_out}": job.check_out.strftime(date_format),
            "{adults}": str(job.adults),
            "{children}": str(job.children),
            "{total_guests}": str(job.total_guests),
            "{child_age_1}": child_age,
        }

        resolved_payload: Dict[str, str] = {}
        for k, v in raw_payload.items():
            val = str(v)
            for placeholder, actual in replacements.items():
                val = val.replace(placeholder, actual)
            resolved_payload[k] = val

        for key in [
            k for k in list(resolved_payload.keys()) if str(k).startswith("childrens[1][")
        ]:
            resolved_payload.pop(key, None)

        if job.children >= 1:
            ages = list(getattr(job, "children_ages", []) or [])
            for idx in range(1, job.children + 1):
                age_val = str(ages[idx - 1]) if (idx - 1) < len(ages) else "6"
                resolved_payload[f"childrens[1][{idx}]"] = age_val

        logger.info(f"Generujem search token cez POST na {token_endpoint}...")

        # Volanie fetch priamo v kontexte stránky so správnymi cookies a headers
        try:
            eval_result = page.evaluate(
                """async ({ endpoint, payload }) => {
                    const formBody = new URLSearchParams(payload).toString();
                    const res = await fetch(endpoint, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest'
                        },
                        body: formBody
                    });
                    const text = await res.text();
                    return { ok: res.ok, status: res.status, text };
                }""",
                {"endpoint": token_endpoint, "payload": resolved_payload},
            )
        except Exception as exc:
            logger.warning(f"Volanie token API zlyhalo ({token_endpoint}): {exc}")
            return False

        response_ok = True
        response_status: Optional[int] = None
        if isinstance(eval_result, dict):
            response_ok = bool(eval_result.get("ok", True))
            response_status = eval_result.get("status")
            response_text = str(eval_result.get("text") or "")
        else:
            response_text = str(eval_result or "")

        logger.debug(f"Odpoveď token API (ok={response_ok}, status={response_status}): {response_text}")

        if not response_ok or not response_text.strip():
            logger.warning(
                f"Token API vrátilo neúspešný status ({response_status}) alebo prázdnu odpoveď: "
                f"{response_text[:200]}"
            )
            return False

        # Extrakcia tokenu (môže to byť UUID reťazec alebo JSON)
        token = ""
        try:
            data = json.loads(response_text)
            if isinstance(data, dict):
                if data.get("success") is False or data.get("status") in ("error", "failed", "unavailable"):
                    logger.warning(f"Token API vrátilo neúspešný JSON stav: {response_text[:200]}")
                    return False
                token = str(data.get("token") or data.get("id") or data.get("search_token") or "").strip()
            elif isinstance(data, str):
                token = data.strip()
        except Exception:
            token = response_text.strip().strip('"')

        # Regex vyhľadanie UUID tokenu ak je odpoveď komplexnejšia
        if not token or len(token) > 100:
            match = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", response_text)
            if match:
                token = match.group(1)

        if not token:
            logger.warning(f"Nepodarilo sa získať search token z odpovede: {response_text[:200]}")
            return False

        target_url = search_template.replace("{token}", token)
        logger.info(f"Získaný search token: {token} -> Navigujem na výsledky: {target_url}")
        try:
            # 'commit' znamená: ako náhle server odpovie hlavičkami a začne tiecť HTML, pokračuj ďalej
            page.goto(target_url, wait_until="commit", timeout=15000)
        except Exception as nav_exc:
            logger.debug(f"Navigácia na token URL hlási stav: {nav_exc}")

        return True

    @staticmethod
    def _build_not_available_result(
        hotel_name: str,
        direct_url: Optional[str] = None,
        error_message: str = UNAVAILABLE_DATE_MESSAGE,
    ) -> ScrapedResult:
        """Create a standardized NOT_AVAILABLE ScrapedResult for unavailable direct dates."""
        return ScrapedResult(
            hotel_name=hotel_name,
            direct_price=None,
            direct_url=direct_url,
            status=ResultStatus.NOT_AVAILABLE,
            is_available=False,
            direct_offers=[],
            error_message=error_message,
        )

    @staticmethod
    def _is_calendar_element_disabled(locator: Locator) -> bool:
        """Return True if a calendar date element is disabled or marked unavailable."""
        try:
            if locator.is_disabled():
                return True
        except Exception:
            pass

        try:
            if locator.get_attribute("disabled") is not None:
                return True
            aria_disabled = (locator.get_attribute("aria-disabled") or "").strip().lower()
            if aria_disabled == "true":
                return True
            class_attr = (locator.get_attribute("class") or "").lower()
            if any(token in class_attr for token in DISABLED_CALENDAR_TOKENS):
                return True
        except Exception:
            return True

        return False

    def _select_calendar_or_form_date(
        self,
        page: Page,
        selector: str,
        date_value: str,
        label: str,
    ) -> bool:
        """Select or fill a date element in a booking calendar/form; returns False if unavailable."""
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                logger.info(f"Element dátumu ({label}) pre selektor '{selector}' neexistuje v kalendári.")
                return False

            if self._is_calendar_element_disabled(locator):
                logger.info(f"Dátum ({label}={date_value}) je v kalendári zablokovaný / disabled ('{selector}').")
                return False

            tag_name = ""
            is_readonly = False
            try:
                tag_name = str(locator.evaluate("(el) => (el.tagName || '').toLowerCase()") or "")
                is_readonly = locator.get_attribute("readonly") is not None
            except Exception:
                tag_name = ""

            if tag_name in ("input", "textarea") and not is_readonly:
                locator.fill(date_value, timeout=3000)
            else:
                locator.click(timeout=3000)
            return True
        except Exception as exc:
            logger.info(f"Dátum ({label}={date_value}) nie je možné vybrať cez '{selector}': {exc}")
            return False

    def _handle_form_or_calendar_interaction(
        self,
        page: Page,
        rules: Dict[str, Any],
        job: Optional[ScrapingJob],
    ) -> bool:
        """Execute optional calendar/form date selection and submit defined in dynamic rules."""
        calendar_open_sel = rules.get("calendar_open_selector")
        calendar_day_sel = rules.get("calendar_day_selector")
        check_in_sel = rules.get("check_in_selector")
        check_out_sel = rules.get("check_out_selector")
        submit_sel = rules.get("submit_selector")

        if not any((calendar_open_sel, calendar_day_sel, check_in_sel, check_out_sel, submit_sel)):
            return True

        date_format = rules.get("date_format", "%Y-%m-%d")
        check_in_str = job.check_in.strftime(date_format) if job is not None else ""
        check_out_str = job.check_out.strftime(date_format) if job is not None else ""

        if calendar_open_sel:
            try:
                open_loc = page.locator(str(calendar_open_sel)).first
                if open_loc.count() > 0:
                    open_loc.click(timeout=3000)
            except Exception as exc:
                logger.info(f"Nepodarilo sa otvoriť rezervačný kalendár '{calendar_open_sel}': {exc}")
                return False

        if calendar_day_sel and job is not None:
            tpl = str(calendar_day_sel)
            cin_selector = tpl.replace("{date}", check_in_str).replace("{check_in}", check_in_str)
            if not self._select_calendar_or_form_date(page, cin_selector, check_in_str, "check_in"):
                return False
            if "{date}" in tpl or "{check_out}" in tpl:
                cout_selector = tpl.replace("{date}", check_out_str).replace("{check_out}", check_out_str)
                if cout_selector != cin_selector:
                    if not self._select_calendar_or_form_date(page, cout_selector, check_out_str, "check_out"):
                        return False

        if check_in_sel and job is not None:
            cin_selector = str(check_in_sel).replace("{check_in}", check_in_str).replace("{date}", check_in_str)
            if not self._select_calendar_or_form_date(page, cin_selector, check_in_str, "check_in"):
                return False

        if check_out_sel and job is not None:
            cout_selector = str(check_out_sel).replace("{check_out}", check_out_str).replace("{date}", check_out_str)
            if not self._select_calendar_or_form_date(page, cout_selector, check_out_str, "check_out"):
                return False

        if submit_sel:
            try:
                submit_loc = page.locator(str(submit_sel)).first
                if submit_loc.count() == 0 or self._is_calendar_element_disabled(submit_loc):
                    logger.info(f"Odosielacie tlačidlo formulára '{submit_sel}' nie je dostupné.")
                    return False
                submit_loc.click(timeout=5000)
            except Exception as exc:
                logger.info(f"Odoslanie rezervačného formulára '{submit_sel}' zlyhalo: {exc}")
                return False

        return True

    def parse_room_catalog(
        self,
        page: Page,
        rules: Dict[str, Any],
        hotel_name: str,
    ) -> List[RoomOffer]:
        """Parse static direct hotel catalog page (without dates or token POST) for room names."""
        effective_rules = (rules.get("rules") if isinstance(rules.get("rules"), dict) else rules) or {}
        try:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass

            self._dismiss_cookie_banners(page, effective_rules)

            wait_sel = effective_rules.get("catalog_wait_for_selector") or effective_rules.get(
                "wait_for_selector"
            )
            if wait_sel:
                try:
                    page.wait_for_selector(wait_sel, timeout=8000)
                except Exception:
                    page.wait_for_timeout(1500)
            else:
                page.wait_for_timeout(500)

            catalog_selectors: List[str] = (
                effective_rules.get("catalog_room_selectors")
                or effective_rules.get("room_name_selectors")
                or [
                    ".room-title",
                    ".room-name",
                    "h2.room-heading",
                    "h3.room-title",
                    ".room-card h2",
                    ".room-card h3",
                    ".room-item h2",
                    ".room-item h3",
                ]
            )

            offers: List[RoomOffer] = []
            seen_names = set()

            targets: List[Any] = [page]
            for frame in page.frames:
                if frame != page.main_frame:
                    targets.append(frame)

            for target in targets:
                for selector in catalog_selectors:
                    try:
                        loc = target.locator(selector)
                        count = loc.count()
                        for i in range(count):
                            el = loc.nth(i)
                            cleaned = self._extract_clean_room_name(el)
                            if not cleaned or len(cleaned) > 120:
                                continue
                            norm = cleaned.lower()
                            if norm not in seen_names:
                                seen_names.add(norm)
                                offers.append(RoomOffer(room_name=cleaned, price=0.0))
                    except Exception as exc:
                        logger.debug(
                            f"Chyba pri katalógovom selektore '{selector}' pre {hotel_name}: {exc}"
                        )
                        continue

            logger.info(
                f"Katalóg priameho webu pre '{hotel_name}': nájdených {len(offers)} unikátnych izieb."
            )
            return offers
        except Exception as exc:
            logger.error(f"Chyba pri parsovaní priameho katalógu pre {hotel_name}: {exc}")
            return []

    @staticmethod
    def _clean_room_name_text(raw_name: str) -> str:
        """Strip nested availability/price badges and normalize whitespace in room names."""
        if not raw_name:
            return ""
        text = str(raw_name)
        text = re.sub(
            r"\s*Počet\s+dostupných\s*:.*$",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(
            r"\s*(?:\b|(?<=\d))od\s+\d+(?:[.,]\d+)?\s*(?:EUR|€).*$",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        return " ".join(text.split()).strip()

    def _extract_clean_room_name(self, locator: Locator) -> str:
        """Extract primary room name text while ignoring nested <small> and <span class='from'> elements."""
        raw_name: Optional[str] = None
        try:
            evaluated = locator.evaluate(
                """(el) => {
                    const clone = el.cloneNode(true);
                    clone.querySelectorAll('small, span.from, .alertonlyone, .from').forEach(n => n.remove());
                    return clone.textContent || '';
                }"""
            )
            if isinstance(evaluated, str) and evaluated.strip():
                raw_name = evaluated
        except Exception:
            raw_name = None

        if not raw_name:
            raw_name = locator.text_content() or ""

        return self._clean_room_name_text(raw_name)

    @staticmethod
    def _normalize_selector_list(raw_selectors: Any) -> List[str]:
        """Normalize a single selector string or list of selectors into a clean list of strings."""
        if isinstance(raw_selectors, str):
            cleaned = raw_selectors.strip()
            return [cleaned] if cleaned else []
        if isinstance(raw_selectors, list):
            return [str(s).strip() for s in raw_selectors if s and str(s).strip()]
        return []

    @staticmethod
    def _adapt_selector_for_offer_item(selector: str, item_selector: str) -> str:
        """Strip leading offer_item selector prefix so card-scoped selectors work inside an offer_item."""
        item_sel = item_selector.strip()
        if not item_sel:
            return selector.strip()
        pattern = re.compile(rf"^(?:[a-zA-Z0-9_-]+)?{re.escape(item_sel)}(?:\s*>\s*|\s+)")
        parts: List[str] = []
        for raw_part in selector.split(","):
            part = raw_part.strip()
            if not part:
                continue
            adapted = pattern.sub("", part, count=1).strip()
            parts.append(adapted or part)
        return ", ".join(parts) if parts else selector.strip()

    def _extract_primary_price_text(self, locator: Locator) -> str:
        """Extract primary price text while stripping nested member/discounted rate elements."""
        raw_text: Optional[str] = None
        try:
            evaluated = locator.evaluate(
                """(el) => {
                    const clone = el.cloneNode(true);
                    clone.querySelectorAll('.member_rate, p.member_rate, .member-rate, .loyalty-price').forEach(n => n.remove());
                    return clone.textContent || '';
                }"""
            )
            if isinstance(evaluated, str) and evaluated.strip():
                raw_text = evaluated
        except Exception:
            raw_text = None

        if not raw_text:
            raw_text = locator.text_content() or ""

        return " ".join(raw_text.split()).strip()

    def _extract_first_valid_price(
        self,
        scope: Locator,
        price_selectors: List[str],
        price_regex: Optional[str],
    ) -> Optional[float]:
        """Extract the first valid main price (>= 20 EUR) from a scope, preferring visible elements."""
        for require_visible in (True, False):
            for p_sel in price_selectors:
                try:
                    p_locs = scope.locator(p_sel)
                    p_count = p_locs.count()
                    for p_idx in range(p_count):
                        p_el = p_locs.nth(p_idx)
                        if require_visible and not p_el.is_visible():
                            continue
                        cleaned_p = self._extract_primary_price_text(p_el)
                        if not cleaned_p or len(cleaned_p) > 80:
                            continue
                        p_val = clean_price_text(cleaned_p, price_regex=price_regex)
                        if p_val is not None and p_val >= 20.0:
                            return p_val
                except Exception:
                    continue
        return None

    def _extract_bonuses_from_scope(
        self,
        scope: Locator,
        bonus_selectors: List[str],
    ) -> Optional[str]:
        """Extract deduplicated bonus / meal plan strings joined by ' | ' from a card or offer item."""
        if not bonus_selectors:
            return None
        bonus_items: List[str] = []
        seen_bonuses = set()
        for b_sel in bonus_selectors:
            try:
                b_locs = scope.locator(b_sel)
                b_count = b_locs.count()
                for b_idx in range(b_count):
                    raw_b = b_locs.nth(b_idx).text_content()
                    if not raw_b:
                        continue
                    cleaned_b = " ".join(raw_b.split())
                    if cleaned_b:
                        norm_b = cleaned_b.lower()
                        if norm_b not in seen_bonuses:
                            seen_bonuses.add(norm_b)
                            bonus_items.append(cleaned_b)
            except Exception:
                continue
        return " | ".join(bonus_items) if bonus_items else None

    def _extract_offer_item_offers(
        self,
        card: Locator,
        room_name: str,
        requested_guests: int,
        explicit_offer_item_selectors: List[str],
        price_selectors: List[str],
        bonus_selectors: List[str],
        price_regex: Optional[str],
    ) -> List[RoomOffer]:
        """Extract individual RoomOffer entries when a room card contains nested rate/package blocks."""
        if explicit_offer_item_selectors:
            for item_sel in explicit_offer_item_selectors:
                try:
                    items_loc = card.locator(item_sel)
                    item_count = items_loc.count()
                    if item_count == 0:
                        continue

                    scoped_price_selectors = [
                        self._adapt_selector_for_offer_item(sel, item_sel)
                        for sel in price_selectors
                    ]
                    scoped_bonus_selectors = [
                        self._adapt_selector_for_offer_item(sel, item_sel)
                        for sel in bonus_selectors
                    ]

                    item_offers: List[RoomOffer] = []
                    for i in range(item_count):
                        item_el = items_loc.nth(i)
                        item_price = self._extract_first_valid_price(
                            item_el, scoped_price_selectors, price_regex
                        )
                        if item_price is None:
                            continue
                        item_bonuses = self._extract_bonuses_from_scope(
                            item_el, scoped_bonus_selectors
                        )
                        item_offers.append(
                            RoomOffer(
                                room_name=room_name,
                                occupancy=requested_guests,
                                price=item_price,
                                currency="EUR",
                                meal_plan=item_bonuses,
                                bonuses=item_bonuses,
                            )
                        )
                    if item_offers:
                        return item_offers
                except Exception:
                    continue

        for item_sel in DEFAULT_OFFER_ITEM_SELECTORS:
            try:
                items_loc = card.locator(item_sel)
                item_count = items_loc.count()
                if item_count <= 1:
                    continue

                scoped_price_selectors = [
                    self._adapt_selector_for_offer_item(sel, item_sel)
                    for sel in price_selectors
                ]
                scoped_bonus_selectors = [
                    self._adapt_selector_for_offer_item(sel, item_sel)
                    for sel in bonus_selectors
                ]

                visible_priced_items: List[Tuple[Locator, float]] = []
                for i in range(item_count):
                    item_el = items_loc.nth(i)
                    try:
                        if not item_el.is_visible():
                            continue
                    except Exception:
                        continue
                    item_price = self._extract_first_valid_price(
                        item_el, scoped_price_selectors, price_regex
                    )
                    if item_price is not None:
                        visible_priced_items.append((item_el, item_price))

                if len(visible_priced_items) > 1:
                    item_offers = []
                    for item_el, item_price in visible_priced_items:
                        item_bonuses = self._extract_bonuses_from_scope(
                            item_el, scoped_bonus_selectors
                        )
                        item_offers.append(
                            RoomOffer(
                                room_name=room_name,
                                occupancy=requested_guests,
                                price=item_price,
                                currency="EUR",
                                meal_plan=item_bonuses,
                                bonuses=item_bonuses,
                            )
                        )
                    return item_offers
            except Exception:
                continue

        return []

    def _extract_direct_offers(
        self,
        target: Any,
        effective_rules: Dict[str, Any],
        requested_guests: int,
    ) -> List[RoomOffer]:
        """Extract room-level offers from room cards using dynamic rules."""
        room_card_selectors: List[str] = effective_rules.get("room_card_selectors", [])
        if not room_card_selectors:
            return []

        room_name_selectors: List[str] = effective_rules.get(
            "room_name_selectors",
            [".room-title", ".room-name", "h2", "h3", "h4"],
        )
        room_name_attr: Optional[str] = effective_rules.get("room_name_attribute")
        bonus_selectors: List[str] = self._normalize_selector_list(
            effective_rules.get("bonus_selectors", [])
        )
        offer_item_selectors: List[str] = self._normalize_selector_list(
            effective_rules.get("offer_item_selectors", [])
        )
        price_selectors: List[str] = effective_rules.get("price_selectors", [])
        price_regex: Optional[str] = effective_rules.get("price_regex")
        if not price_selectors:
            return []

        extracted_offers: List[RoomOffer] = []
        for card_sel in room_card_selectors:
            try:
                cards = target.locator(card_sel)
                card_count = cards.count()
                if card_count == 0:
                    continue

                for idx in range(card_count):
                    card = cards.nth(idx)
                    room_name: Optional[str] = None

                    if isinstance(room_name_attr, str) and room_name_attr.strip():
                        attr_name = room_name_attr.strip()
                        try:
                            raw_attr = card.get_attribute(attr_name)
                            if not raw_attr:
                                attr_loc = card.locator(f"[{attr_name}]").first
                                if attr_loc.count() > 0:
                                    raw_attr = attr_loc.get_attribute(attr_name)
                            if raw_attr:
                                cleaned_attr = " ".join(raw_attr.split())
                                if (
                                    cleaned_attr
                                    and cleaned_attr.strip() not in ("", "—")
                                    and len(cleaned_attr) <= 120
                                ):
                                    room_name = cleaned_attr
                        except Exception:
                            pass

                    if not room_name:
                        for n_sel in room_name_selectors:
                            try:
                                n_loc = card.locator(n_sel).first
                                if n_loc.count() > 0:
                                    cleaned_name = self._extract_clean_room_name(n_loc)
                                    if (
                                        cleaned_name
                                        and cleaned_name.strip() not in ("", "—")
                                        and len(cleaned_name) <= 120
                                    ):
                                        room_name = cleaned_name
                                        break
                            except Exception:
                                continue

                    if not room_name or room_name.strip() in ("", "—"):
                        continue

                    nested_offers = self._extract_offer_item_offers(
                        card=card,
                        room_name=room_name,
                        requested_guests=requested_guests,
                        explicit_offer_item_selectors=offer_item_selectors,
                        price_selectors=price_selectors,
                        bonus_selectors=bonus_selectors,
                        price_regex=price_regex,
                    )
                    if nested_offers:
                        extracted_offers.extend(nested_offers)
                        continue

                    card_prices: List[float] = []
                    for p_sel in price_selectors:
                        try:
                            p_locs = card.locator(p_sel)
                            p_count = p_locs.count()
                            for p_idx in range(p_count):
                                p_el = p_locs.nth(p_idx)
                                if p_el.is_visible():
                                    cleaned_p = self._extract_primary_price_text(p_el)
                                    if not cleaned_p or len(cleaned_p) > 80:
                                        continue
                                    p_val = clean_price_text(cleaned_p, price_regex=price_regex)
                                    if p_val is not None and p_val >= 20.0:
                                        card_prices.append(p_val)
                        except Exception:
                            continue

                    if not card_prices:
                        for p_sel in price_selectors:
                            try:
                                p_locs = card.locator(p_sel)
                                p_count = p_locs.count()
                                for p_idx in range(p_count):
                                    cleaned_p = self._extract_primary_price_text(p_locs.nth(p_idx))
                                    if not cleaned_p or len(cleaned_p) > 80:
                                        continue
                                    p_val = clean_price_text(cleaned_p, price_regex=price_regex)
                                    if p_val is not None and p_val >= 20.0:
                                        card_prices.append(p_val)
                            except Exception:
                                continue

                    bonuses_text = self._extract_bonuses_from_scope(card, bonus_selectors)

                    if card_prices:
                        extracted_offers.append(
                            RoomOffer(
                                room_name=room_name,
                                occupancy=requested_guests,
                                price=min(card_prices),
                                currency="EUR",
                                meal_plan=bonuses_text,
                                bonuses=bonuses_text,
                            )
                        )

                if extracted_offers:
                    break
            except Exception as exc:
                logger.debug(f"Chyba pri parsovaní karty izby '{card_sel}': {exc}")
                continue

        return extracted_offers

    def parse_direct_hotel(
        self,
        page: Page,
        rules: Dict[str, Any],
        hotel_name: str,
        direct_url: Optional[str] = None,
        job: Optional[ScrapingJob] = None,
    ) -> ScrapedResult:
        """Entrypoint for direct hotel parsing."""
        return self._parse_direct_page_internal(
            page=page,
            rules=rules,
            hotel_name=hotel_name,
            direct_url=direct_url,
            job=job,
        )

    def parse_direct_page(
        self,
        page: Page,
        rules: Dict[str, Any],
        hotel_name: str,
        direct_url: Optional[str] = None,
        job: Optional[ScrapingJob] = None,
    ) -> ScrapedResult:
        """Alias matching parse_direct_hotel."""
        return self._parse_direct_page_internal(
            page=page,
            rules=rules,
            hotel_name=hotel_name,
            direct_url=direct_url,
            job=job,
        )

    def _parse_direct_page_internal(
        self,
        page: Page,
        rules: Dict[str, Any],
        hotel_name: str,
        direct_url: Optional[str] = None,
        job: Optional[ScrapingJob] = None,
    ) -> ScrapedResult:
        """Defensively parse direct page with dynamic rules and form interaction."""
        effective_rules = rules.get("rules") if isinstance(rules.get("rules"), dict) else rules
        method = effective_rules.get("method")

        logger.info(
            f"DirectParser spustený pre '{hotel_name}': method='{method}', "
            f"job={'OK' if job is not None else 'None'}"
        )

        try:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass

            self._dismiss_cookie_banners(page, effective_rules)

            # 1. Vygenerovanie tokenu a prechod na výsledky
            if method == "token_api_post":
                if job is None:
                    logger.error(
                        f"KRITICKÁ CHYBA: Engine pre '{hotel_name}' je 'token_api_post', "
                        f"ale parameter 'job' prišiel ako None! Skontroluj volanie v engine.py."
                    )
                else:
                    token_ok = self._handle_token_api_post(page, effective_rules, job)
                    if not token_ok:
                        logger.info(
                            f"Hotel {hotel_name}: token API nevrátilo platný token / výsledky "
                            f"(označené ako NOT_AVAILABLE)."
                        )
                        return self._build_not_available_result(
                            hotel_name=hotel_name,
                            direct_url=page.url or direct_url,
                        )

            # 1b. Voliteľná interakcia s kalendárom / rezervačným formulárom
            if not self._handle_form_or_calendar_interaction(page, effective_rules, job):
                logger.info(
                    f"Hotel {hotel_name}: termín nie je možné vybrať v rezervačnom kalendári/formulári "
                    f"(označené ako NOT_AVAILABLE)."
                )
                return self._build_not_available_result(
                    hotel_name=hotel_name,
                    direct_url=page.url or direct_url,
                )

            # 2. Defenzívne čakanie na výsledkové elementy
            wait_sel = effective_rules.get("wait_for_selector")
            wait_timeout = int(effective_rules.get("wait_timeout_ms", 10000))
            wait_after_submit = effective_rules.get("wait_after_submit")
            if wait_sel:
                try:
                    page.wait_for_selector(wait_sel, timeout=wait_timeout)
                except Exception:
                    fallback_wait = int(wait_after_submit) if wait_after_submit is not None else 4000
                    if fallback_wait > 0:
                        page.wait_for_timeout(fallback_wait)
            elif wait_after_submit is not None:
                wait_ms = int(wait_after_submit)
                if wait_ms > 0:
                    page.wait_for_timeout(wait_ms)
            else:
                page.wait_for_timeout(3000)

            self._dismiss_cookie_banners(page, effective_rules)

            # 3. Kontrola vypredania
            sold_out_selectors: List[str] = effective_rules.get("sold_out_selectors", [])
            for selector in sold_out_selectors:
                try:
                    locator = page.locator(selector)
                    count = locator.count()
                    if count > 0 and any(locator.nth(i).is_visible() for i in range(count)):
                        logger.info(f"Hotel {hotel_name} je na priamom webe vypredaný.")
                        return self._build_not_available_result(
                            hotel_name=hotel_name,
                            direct_url=page.url or direct_url,
                        )
                except Exception as exc:
                    logger.debug(f"Error checking sold_out_selector '{selector}': {exc}")

            # 4. Extrakcia cien a izieb (bez globálneho fallbacku cien mimo kariet izieb)
            requested_guests = job.adults if job is not None else 2

            raw_direct_offers: List[RoomOffer] = self._extract_direct_offers(
                page, effective_rules, requested_guests
            )
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                raw_direct_offers.extend(
                    self._extract_direct_offers(frame, effective_rules, requested_guests)
                )

            direct_offers: List[RoomOffer] = [
                offer
                for offer in raw_direct_offers
                if getattr(offer, "room_name", None)
                and offer.room_name.strip() not in ("", "—")
                and offer.price is not None
                and offer.price >= 20.0
            ]

            if direct_offers:
                final_direct_price = min(offer.price for offer in direct_offers)
                logger.info(
                    f"Úspešne extrahovaná priama cena pre {hotel_name}: {final_direct_price:.2f} EUR "
                    f"(izieb: {len(direct_offers)}, URL: {page.url})"
                )
                return ScrapedResult(
                    hotel_name=hotel_name,
                    direct_price=final_direct_price,
                    direct_url=page.url,
                    status=ResultStatus.SUCCESS,
                    is_available=True,
                    direct_offers=direct_offers,
                    error_message=None,
                )

            # Žiadne platné ponuky s konkrétnym názvom izby -> nedostupné / vypredané
            logger.info(
                f"Pre {hotel_name} sa nenašli žiadne ponuky izieb s názvom na priamom webe "
                f"(označené ako NOT_AVAILABLE, URL: {page.url})."
            )
            return self._build_not_available_result(
                hotel_name=hotel_name,
                direct_url=page.url or direct_url,
            )

        except Exception as exc:
            logger.error(f"Neočakávaná chyba pri parsovaní priameho webu ({hotel_name}): {exc}")
            self._save_diagnostic_screenshot(page, hotel_name)
            return ScrapedResult(
                hotel_name=hotel_name,
                direct_price=None,
                direct_url=page.url,
                is_available=False,
                error_message=str(exc),
            )
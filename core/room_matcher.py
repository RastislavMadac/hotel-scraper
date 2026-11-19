"""Room catalog classification, fuzzy matching, and deterministic runtime mapping."""

from dataclasses import replace
from datetime import datetime
import re
from typing import Any, Dict, List, Optional, Tuple
import unicodedata

from thefuzz import fuzz

from core.logger import logger
from core.models import RoomOffer

DEFAULT_PRIORITY_ORDER: List[str] = [
    "family",
    "suite",
    "2_plus_1",
    "1_pax",
    "2_pax",
]

DEFAULT_CAPACITY_KEYWORDS: Dict[str, List[str]] = {
    "family": [
        "family",
        "rodinn",
        "2+2",
        "4 osob",
        "4 lozk",
        "4-lozk",
        "stvorlozk",
        "quadruple",
        "mezonet",
        "pre 4",
    ],
    "suite": [
        "suite",
        "suita",
        "apartman",
        "penthouse",
        "prezidentsk",
        "president",
        "junior suite",
        "deluxe suite",
        "studio",
    ],
    "1_pax": [
        "1-lozkov",
        "1 lozkov",
        "jednolozkov",
        "single",
        "1-lozk",
        "1 lozk",
        "1 osob",
        "pre 1",
    ],
    "2_plus_1": [
        "pristel",
        "pristelk",
        "plus",
        "pohovk",
        "sofa",
        "2+1",
        "3 osob",
        "3 lozk",
        "3-lozk",
        "trojlozk",
        "triple",
        "extra bed",
        "rozkladac",
    ],
    "2_pax": [
        "dvojlozk",
        "2 osob",
        "2 lozk",
        "2-lozk",
        "double",
        "twin",
        "manzelsk",
        "king",
        "queen",
        "pre 2",
    ],
}

DEFAULT_MIN_FUZZY_SCORE: int = 55
DEFAULT_EXCLUSIVE_DIRECT_GROUPS: List[str] = ["family"]
DEFAULT_STOP_WORDS: List[str] = [
    "izba",
    "room",
    "suite",
    "suita",
    "1-lozkova",
    "2-lozkova",
    "s pristelkou",
    "s balkonom",
    "bez balkona",
    "s vyhladom na hory",
    "s vyhladom",
    "na hory",
    "plus",
]


class RoomMatcher:
    """Handles 3-layer room catalog matching and deterministic daily mapping application."""

    @staticmethod
    def strip_diacritics(text: str) -> str:
        """Remove diacritical marks from text while preserving ASCII characters."""
        if not text:
            return ""
        nfkd = unicodedata.normalize("NFKD", str(text))
        return "".join(ch for ch in nfkd if not unicodedata.combining(ch))

    @classmethod
    def _normalize_plain(cls, text: Optional[str]) -> str:
        """Normalize text via NFKD diacritic removal, lowercasing, and whitespace cleanup."""
        if not text:
            return ""
        return " ".join(cls.strip_diacritics(str(text)).lower().split())

    @classmethod
    def normalize_text(
        cls,
        text: str,
        synonyms: Optional[Dict[str, str]] = None,
        stop_words: Optional[List[str]] = None,
    ) -> str:
        """Normalize room title for fuzzy comparison with optional synonyms and stop words."""
        if not text:
            return ""

        base = cls.strip_diacritics(text).lower()
        base = re.sub(r"[^a-z0-9\s]", " ", base)
        joined = " ".join(base.split())

        if isinstance(synonyms, dict) and synonyms:
            norm_synonyms: Dict[str, str] = {}
            for k, v in synonyms.items():
                k_clean = " ".join(
                    re.sub(r"[^a-z0-9\s]", " ", cls.strip_diacritics(str(k)).lower()).split()
                )
                v_clean = " ".join(
                    re.sub(r"[^a-z0-9\s]", " ", cls.strip_diacritics(str(v)).lower()).split()
                )
                if k_clean:
                    norm_synonyms[k_clean] = v_clean

            for src, dst in norm_synonyms.items():
                joined = re.sub(rf"\b{re.escape(src)}\b", dst, joined)
            joined = " ".join(joined.split())

        raw_stops: List[str] = list(DEFAULT_STOP_WORDS)
        if isinstance(stop_words, (list, tuple, set)):
            raw_stops.extend(str(w) for w in stop_words)

        phrase_stops: List[str] = []
        single_stops = set()
        seen_phrases = set()
        for w in raw_stops:
            w_clean = " ".join(
                re.sub(r"[^a-z0-9\s]", " ", cls.strip_diacritics(str(w)).lower()).split()
            )
            if not w_clean:
                continue
            if " " in w_clean:
                if w_clean not in seen_phrases:
                    seen_phrases.add(w_clean)
                    phrase_stops.append(w_clean)
            else:
                single_stops.add(w_clean)

        phrase_stops.sort(key=len, reverse=True)
        for phrase in phrase_stops:
            joined = re.sub(rf"\b{re.escape(phrase)}\b", " ", joined)

        tokens = [t for t in joined.split() if t and t not in single_stops]
        return " ".join(tokens)

    @staticmethod
    def _norm_exact(text: Optional[str]) -> str:
        """Normalize whitespace and casing for deterministic exact dictionary lookups."""
        if not text:
            return ""
        return " ".join(str(text).split()).lower()

    @classmethod
    def merge_rules(
        cls,
        global_rules: Optional[Dict[str, Any]] = None,
        hotel_rules: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Merge default, global ('room_matching'), and per-hotel ('matching_rules') configs."""
        merged: Dict[str, Any] = {
            "capacity_priority_order": list(DEFAULT_PRIORITY_ORDER),
            "capacity_keywords": {k: list(v) for k, v in DEFAULT_CAPACITY_KEYWORDS.items()},
            "min_fuzzy_score": DEFAULT_MIN_FUZZY_SCORE,
            "exclusive_direct_groups": list(DEFAULT_EXCLUSIVE_DIRECT_GROUPS),
            "synonyms": {},
            "stop_words": list(DEFAULT_STOP_WORDS),
        }

        for layer_idx, layer in enumerate((global_rules, hotel_rules)):
            if not isinstance(layer, dict) or not layer:
                continue
            is_global_layer = layer_idx == 0
            effective = layer.get("rules") if isinstance(layer.get("rules"), dict) else layer

            if isinstance(effective.get("capacity_priority_order"), list):
                merged["capacity_priority_order"] = list(
                    effective["capacity_priority_order"]
                )

            if isinstance(effective.get("capacity_keywords"), dict):
                for grp, kws in effective["capacity_keywords"].items():
                    if isinstance(kws, list):
                        merged["capacity_keywords"][grp] = list(kws)

            if effective.get("min_fuzzy_score") is not None:
                try:
                    parsed_score = int(effective["min_fuzzy_score"])
                    if is_global_layer and parsed_score == 70:
                        merged["min_fuzzy_score"] = DEFAULT_MIN_FUZZY_SCORE
                    else:
                        merged["min_fuzzy_score"] = parsed_score
                except (TypeError, ValueError):
                    pass

            if isinstance(effective.get("exclusive_direct_groups"), list):
                merged["exclusive_direct_groups"] = list(effective["exclusive_direct_groups"])

            if isinstance(effective.get("synonyms"), dict):
                merged["synonyms"].update(effective["synonyms"])

            extra_stops: List[str] = []
            if isinstance(effective.get("stop_words"), list):
                extra_stops.extend(str(w) for w in effective["stop_words"])
            if isinstance(effective.get("normalization_stop_words"), list):
                extra_stops.extend(str(w) for w in effective["normalization_stop_words"])
            if extra_stops:
                merged["stop_words"] = list(
                    dict.fromkeys([*merged["stop_words"], *extra_stops])
                )

        return merged

    @classmethod
    def _keyword_matches(cls, text_norm: str, keyword: str) -> bool:
        """Check whether keyword stem/token matches normalized room + bed text."""
        kw = cls._normalize_plain(str(keyword))
        if not kw:
            return False

        pattern = rf"(?:^|[\s\-_/,(+]){re.escape(kw)}"
        if kw[-1].isdigit():
            pattern += r"(?!\d)"
        return re.search(pattern, text_norm) is not None

    @classmethod
    def classify_capacity_group(
        cls,
        room_name: str,
        bed_info: Optional[str] = None,
        rules: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Classify a room into a standardized capacity group using priority order."""
        effective = rules or cls.merge_rules()
        priority_order: List[str] = effective.get(
            "capacity_priority_order", DEFAULT_PRIORITY_ORDER
        )
        keywords_map: Dict[str, List[str]] = effective.get(
            "capacity_keywords", DEFAULT_CAPACITY_KEYWORDS
        )

        name_norm = cls._normalize_plain(room_name)
        bed_norm = cls._normalize_plain(bed_info)
        combined_norm = f"{name_norm} {bed_norm}".strip()

        def _is_family() -> bool:
            if "family" in name_norm or "rodinn" in name_norm:
                return True
            return any(
                cls._keyword_matches(combined_norm, kw)
                for kw in keywords_map.get("family", [])
            )

        def _is_suite() -> bool:
            if any(
                marker in name_norm
                for marker in (
                    "suite",
                    "suita",
                    "apartman",
                    "penthouse",
                    "president",
                    "prezidentsk",
                )
            ):
                return True
            return any(
                cls._keyword_matches(name_norm, kw)
                for kw in keywords_map.get("suite", [])
            )

        def _is_1_pax() -> bool:
            if name_norm.startswith(("1-lozkov", "1 lozkov", "jednolozkov", "single")):
                return True
            has_twin_marker = (
                re.search(
                    r"(?:\bdvojlozk|\b2\s+jednolozk|\bjednolozkov(?:e|ymi|ych)\s+postel)",
                    name_norm,
                )
                is not None
            )
            if not has_twin_marker and (
                "1-lozkov" in name_norm or "jednolozkov" in name_norm
            ):
                return True
            # Single-bed descriptions take priority over sofa/2_plus_1 unless a double bed is present
            if (
                "jednolozkova postel" in bed_norm or "1 jednolozkov" in bed_norm
            ) and "manzelsk" not in bed_norm:
                return True
            if not has_twin_marker and "manzelsk" not in bed_norm:
                return any(
                    cls._keyword_matches(name_norm, kw)
                    for kw in keywords_map.get("1_pax", [])
                )
            return False

        def _is_2_plus_1() -> bool:
            if _is_family() or _is_suite() or _is_1_pax():
                return False
            if "pristel" in name_norm or re.search(r"\bplus\b", name_norm) is not None:
                return True
            if any(
                marker in bed_norm
                for marker in ("pohovk", "sofa", "pristel", "rozkladac", "extra bed")
            ):
                return True
            for kw in keywords_map.get("2_plus_1", []):
                kw_norm = cls._normalize_plain(str(kw))
                if kw_norm == "plus":
                    if re.search(r"\bplus\b", combined_norm) is not None:
                        return True
                elif cls._keyword_matches(combined_norm, kw):
                    return True
            return False

        def _matches_group(group: str) -> bool:
            if group == "family":
                return _is_family()
            if group == "suite":
                return _is_suite()
            if group == "1_pax":
                return _is_1_pax()
            if group == "2_plus_1":
                return _is_2_plus_1()
            if _is_family() or _is_suite() or _is_1_pax() or _is_2_plus_1():
                return False
            return any(
                cls._keyword_matches(combined_norm, kw)
                for kw in keywords_map.get(group, [])
            )

        for group in priority_order:
            if _matches_group(group):
                return group

        return "2_pax"

    def _compute_fuzzy_score(
        self,
        direct_name: str,
        booking_name: str,
        synonyms: Dict[str, str],
        stop_words: List[str],
    ) -> int:
        """Compute token similarity prioritizing normalized distinguishing room attributes."""
        d_raw = self.strip_diacritics(direct_name).lower()
        b_raw = self.strip_diacritics(booking_name).lower()
        d_clean = self.normalize_text(direct_name, synonyms=synonyms, stop_words=stop_words)
        b_clean = self.normalize_text(booking_name, synonyms=synonyms, stop_words=stop_words)

        raw_sort = fuzz.token_sort_ratio(d_raw, b_raw)
        raw_set = fuzz.token_set_ratio(d_raw, b_raw)
        raw_blend = round(0.6 * raw_sort + 0.4 * raw_set)
        scores = [raw_sort, raw_blend]
        if d_clean and b_clean:
            clean_sort = fuzz.token_sort_ratio(d_clean, b_clean)
            clean_set = fuzz.token_set_ratio(d_clean, b_clean)
            clean_blend = round(0.6 * clean_sort + 0.4 * clean_set)
            scores.append(clean_blend)
        return int(max(scores))

    def build_catalog_mappings(
        self,
        hotel: Dict[str, Any],
        booking_offers: List[RoomOffer],
        direct_offers: List[RoomOffer],
        global_rules: Optional[Dict[str, Any]] = None,
        existing_mappings: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Build N:1 room mappings within capacity groups for persistence in hs-room_mappings."""
        hotel_id = str(hotel.get("id", ""))
        hotel_rules = hotel.get("matching_rules") or {}
        rules = self.merge_rules(global_rules, hotel_rules)

        min_fuzzy_score = int(rules.get("min_fuzzy_score", DEFAULT_MIN_FUZZY_SCORE))
        exclusive_groups = set(
            rules.get("exclusive_direct_groups", DEFAULT_EXCLUSIVE_DIRECT_GROUPS)
        )
        synonyms: Dict[str, str] = rules.get("synonyms") or {}
        stop_words: List[str] = rules.get("stop_words") or []

        # Index existing mappings by normalized direct_room_pattern
        existing_by_pattern: Dict[str, Dict[str, Any]] = {}
        if isinstance(existing_mappings, list):
            for rec in existing_mappings:
                if isinstance(rec, dict):
                    pat_key = self._norm_exact(rec.get("direct_room_pattern"))
                    if pat_key and pat_key not in existing_by_pattern:
                        existing_by_pattern[pat_key] = rec

        # Deduplicate Booking offers and group strictly by capacity_group
        booking_by_group: Dict[str, List[RoomOffer]] = {}
        booking_by_name: Dict[str, RoomOffer] = {}
        seen_booking = set()
        for b_off in booking_offers:
            b_norm = self._norm_exact(b_off.room_name)
            if not b_norm or b_norm in seen_booking:
                continue
            seen_booking.add(b_norm)
            booking_by_name[b_norm] = b_off
            b_group = self.classify_capacity_group(b_off.room_name, b_off.bed_info, rules)
            booking_by_group.setdefault(b_group, []).append(b_off)

        # Deduplicate direct offers preserving order
        unique_direct: List[RoomOffer] = []
        seen_direct = set()
        for d_off in direct_offers:
            d_norm = self._norm_exact(d_off.room_name)
            if not d_norm or d_norm in seen_direct:
                continue
            seen_direct.add(d_norm)
            unique_direct.append(d_off)

        now_iso = datetime.utcnow().isoformat()
        results: List[Dict[str, Any]] = []
        handled_patterns = set()

        for priority_idx, d_off in enumerate(unique_direct, start=1):
            d_name = " ".join(d_off.room_name.split())
            d_norm = self._norm_exact(d_name)
            handled_patterns.add(d_norm)
            existing = existing_by_pattern.get(d_norm)

            if existing is not None and existing.get("is_manual_override") is True:
                cap_group = existing.get("capacity_group") or self.classify_capacity_group(
                    d_name, d_off.bed_info, rules
                )
                b_room_name = existing.get("booking_room_name")
                b_bed_info = existing.get("booking_bed_info")
                if not b_bed_info and b_room_name:
                    matched_b = booking_by_name.get(self._norm_exact(b_room_name))
                    if matched_b and matched_b.bed_info:
                        b_bed_info = matched_b.bed_info
                is_exclusive = existing.get("is_exclusive_direct", b_room_name is None)
                score = existing.get("match_score")
                if score is None:
                    score = 100 if b_room_name else 0
                results.append(
                    {
                        "hotel_id": hotel_id,
                        "capacity_group": cap_group,
                        "direct_room_pattern": existing.get("direct_room_pattern") or d_name,
                        "booking_room_name": b_room_name,
                        "booking_bed_info": b_bed_info if b_bed_info else None,
                        "match_score": int(score),
                        "is_exclusive_direct": bool(is_exclusive),
                        "is_manual_override": True,
                        "priority": existing.get("priority", priority_idx),
                        "is_active": existing.get("is_active", True),
                        "last_synced_at": now_iso,
                    }
                )
                continue

            d_group = self.classify_capacity_group(d_name, d_off.bed_info, rules)
            if d_group in exclusive_groups or d_group == "family":
                results.append(
                    {
                        "hotel_id": hotel_id,
                        "capacity_group": d_group,
                        "direct_room_pattern": d_name,
                        "booking_room_name": None,
                        "booking_bed_info": None,
                        "match_score": 0,
                        "is_exclusive_direct": True,
                        "is_manual_override": False,
                        "priority": priority_idx,
                        "is_active": True,
                        "last_synced_at": now_iso,
                    }
                )
                continue

            candidates = booking_by_group.get(d_group, [])
            best_candidate: Optional[RoomOffer] = None
            best_score = -1

            for b_cand in candidates:
                score = self._compute_fuzzy_score(
                    d_name, b_cand.room_name, synonyms=synonyms, stop_words=stop_words
                )
                if score > best_score:
                    best_score = score
                    best_candidate = b_cand

            if best_candidate is not None and (
                best_score >= min_fuzzy_score or d_group != "family"
            ):
                if best_score < min_fuzzy_score:
                    logger.debug(
                        f"N:1 fallback v skupine '{d_group}' pre '{d_name}' -> "
                        f"'{best_candidate.room_name}' (skóre {best_score} < {min_fuzzy_score})."
                    )
                results.append(
                    {
                        "hotel_id": hotel_id,
                        "capacity_group": d_group,
                        "direct_room_pattern": d_name,
                        "booking_room_name": best_candidate.room_name,
                        "booking_bed_info": (
                            best_candidate.bed_info if best_candidate.bed_info else None
                        ),
                        "match_score": int(max(0, best_score)),
                        "is_exclusive_direct": False,
                        "is_manual_override": False,
                        "priority": priority_idx,
                        "is_active": True,
                        "last_synced_at": now_iso,
                    }
                )
            else:
                results.append(
                    {
                        "hotel_id": hotel_id,
                        "capacity_group": d_group,
                        "direct_room_pattern": d_name,
                        "booking_room_name": None,
                        "booking_bed_info": None,
                        "match_score": 0,
                        "is_exclusive_direct": True,
                        "is_manual_override": False,
                        "priority": priority_idx,
                        "is_active": True,
                        "last_synced_at": now_iso,
                    }
                )

        # Preserve any manual-override records not seen in current direct catalog
        for pat_key, existing in existing_by_pattern.items():
            if pat_key not in handled_patterns and existing.get("is_manual_override") is True:
                preserved = dict(existing)
                preserved["hotel_id"] = existing.get("hotel_id", hotel_id)
                preserved["last_synced_at"] = now_iso
                results.append(preserved)

        logger.debug(
            f"RoomMatcher pre hotel '{hotel.get('name', hotel_id)}' vygeneroval {len(results)} mapovaní."
        )
        return results

    def apply_saved_mappings(
        self,
        booking_offers: List[RoomOffer],
        direct_offers: List[RoomOffer],
        saved_mappings: List[Dict[str, Any]],
    ) -> Tuple[List[RoomOffer], List[RoomOffer]]:
        """Deterministically apply saved hs-room_mappings without runtime fuzzy guessing."""
        if not isinstance(saved_mappings, list) or not saved_mappings:
            return (list(booking_offers), list(direct_offers))

        active_mappings = sorted(
            [m for m in saved_mappings if isinstance(m, dict) and m.get("is_active", True)],
            key=lambda m: m.get("priority") if isinstance(m.get("priority"), (int, float)) else 100,
        )

        mapping_by_direct: Dict[str, Dict[str, Any]] = {}
        mappings_by_booking: Dict[str, List[Dict[str, Any]]] = {}

        for m in active_mappings:
            d_pat = self._norm_exact(m.get("direct_room_pattern") or m.get("direct_room_name"))
            if d_pat and d_pat not in mapping_by_direct:
                mapping_by_direct[d_pat] = m

            b_pat = self._norm_exact(m.get("booking_room_name"))
            if b_pat:
                mappings_by_booking.setdefault(b_pat, []).append(m)

        direct_offers_by_name: Dict[str, List[RoomOffer]] = {}
        updated_direct: List[RoomOffer] = []
        for d_off in direct_offers:
            d_norm = self._norm_exact(d_off.room_name)
            direct_offers_by_name.setdefault(d_norm, []).append(d_off)

            matched_map = mapping_by_direct.get(d_norm)
            if matched_map is not None:
                raw_score = matched_map.get("match_score")
                score_val = int(raw_score) if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool) else None
                updated_direct.append(
                    replace(
                        d_off,
                        capacity_group=matched_map.get("capacity_group") or d_off.capacity_group,
                        matched_room_name=matched_map.get("booking_room_name") or d_off.matched_room_name,
                        match_score=score_val if score_val is not None else d_off.match_score,
                    )
                )
            else:
                updated_direct.append(d_off)

        updated_booking: List[RoomOffer] = []
        for b_off in booking_offers:
            b_norm = self._norm_exact(b_off.room_name)
            candidate_maps = mappings_by_booking.get(b_norm, [])
            if not candidate_maps:
                updated_booking.append(b_off)
                continue

            best_direct_offer: Optional[RoomOffer] = None
            best_mapping: Optional[Dict[str, Any]] = None

            for m in candidate_maps:
                d_pat = self._norm_exact(m.get("direct_room_pattern") or m.get("direct_room_name"))
                for d_cand in direct_offers_by_name.get(d_pat, []):
                    if best_direct_offer is None:
                        best_direct_offer = d_cand
                        best_mapping = m
                    elif d_cand.price > 0 and (
                        best_direct_offer.price <= 0 or d_cand.price < best_direct_offer.price
                    ):
                        best_direct_offer = d_cand
                        best_mapping = m

            if best_direct_offer is not None and best_mapping is not None:
                raw_score = best_mapping.get("match_score")
                score_val = int(raw_score) if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool) else None
                direct_bonuses = (
                    best_direct_offer.bonuses
                    if best_direct_offer.bonuses is not None
                    else best_direct_offer.meal_plan
                )
                updated_booking.append(
                    replace(
                        b_off,
                        capacity_group=best_mapping.get("capacity_group") or b_off.capacity_group,
                        matched_room_name=best_direct_offer.room_name,
                        matched_direct_price=(
                            float(best_direct_offer.price)
                            if best_direct_offer.price is not None and best_direct_offer.price > 0
                            else None
                        ),
                        match_score=score_val if score_val is not None else b_off.match_score,
                        matched_direct_bonuses=(
                            direct_bonuses
                            if direct_bonuses is not None
                            else b_off.matched_direct_bonuses
                        ),
                    )
                )
            else:
                fallback_map = candidate_maps[0]
                raw_score = fallback_map.get("match_score")
                score_val = int(raw_score) if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool) else None
                updated_booking.append(
                    replace(
                        b_off,
                        capacity_group=fallback_map.get("capacity_group") or b_off.capacity_group,
                        matched_room_name=(
                            fallback_map.get("direct_room_pattern")
                            or fallback_map.get("direct_room_name")
                            or b_off.matched_room_name
                        ),
                        match_score=score_val if score_val is not None else b_off.match_score,
                    )
                )

        return (updated_booking, updated_direct)


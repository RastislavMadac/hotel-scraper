"""Configuration manager for dynamic retrieval of selectors and target hotels.

Follows the Zero-Update architecture: fetches runtime scraping configurations
and target hotel lists from Supabase with fallback to local persistent cache.
"""

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

from config.settings import get_settings
from core.logger import logger


class ConfigError(Exception):
    """Raised when configuration cannot be loaded from remote or local cache."""
    pass


PRICE_COMPARISONS_CONFLICT_COLS = (
    "scraped_date,hotel_id,check_in,check_out,adults,children,"
    "children_ages,channel,room_name,price"
)


class ConfigManager:
    """Manages remote Supabase configuration retrieval and local caching."""

    def __init__(
        self,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
        supabase_client: Optional[Any] = None,
        cache_dir: Optional[Union[str, Path]] = None,
        timeout: float = 5.0,
    ) -> None:
        """Initialize ConfigManager.

        Args:
            supabase_url: Supabase REST base URL.
            supabase_key: Supabase Anon or Service Role key.
            supabase_client: Optional existing client instance.
            cache_dir: Directory where JSON caches are persisted.
            timeout: Network request timeout in seconds.
        """
        settings = get_settings()

        self.supabase_url = (
            supabase_url
            or os.getenv("SUPABASE_URL")
            or settings.supabase_url
        ).rstrip("/")
        self.supabase_key = (
            supabase_key
            or os.getenv("SUPABASE_KEY")
            or os.getenv("SUPABASE_ANON_KEY")
            or settings.supabase_key
        )
        self.supabase_client = supabase_client
        self.timeout = timeout

        # Resolve cache directory
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir)
        else:
            self.cache_dir = settings.cache_dir

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.config_cache_file = self.cache_dir / "config_cache.json"
        self.hotels_cache_file = self.cache_dir / "hotels_cache.json"
        self.room_mappings_cache_file = self.cache_dir / "room_mappings_cache.json"

    @property
    def is_configured(self) -> bool:
        """Verify if Supabase credentials or client are available."""
        if self.supabase_client is not None:
            return True
        return bool(self.supabase_url and self.supabase_key)

    # -------------------------------------------------------------------------
    # Local Cache Helpers
    # -------------------------------------------------------------------------

    def _read_json_cache(self, file_path: Path) -> Optional[Any]:
        """Safely read and parse local JSON cache file."""
        if not file_path.exists():
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning(f"Failed to read local cache {file_path}: {exc}")
            return None

    def _write_json_cache(self, file_path: Path, data: Any) -> None:
        """Atomically persist data to local JSON cache."""
        try:
            temp_path = file_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            temp_path.replace(file_path)
        except Exception as exc:
            logger.warning(f"Failed to write local cache {file_path}: {exc}")

    # -------------------------------------------------------------------------
    # Scraping Rules
    # -------------------------------------------------------------------------

    def get_scraping_rules(self, engine_name: str) -> Dict[str, Any]:
        """Fetch scraping rules and selectors for the specified engine.

        Tries Supabase 'hs_scraping_configs' table first. On network/service failure,
        falls back to 'config_cache.json'. Raises ConfigError if both fail.

        Args:
            engine_name: Target parser engine (e.g. 'booking', 'hoteladmin').

        Returns:
            Dictionary containing selectors, regexes, and field schema.
        """
        if not engine_name or not isinstance(engine_name, str):
            raise ValueError("engine_name must be a non-empty string.")

        remote_rules = self._fetch_rules_from_remote(engine_name)
        if remote_rules is not None:
            # Update cache file with new rules
            self._update_rules_cache(engine_name, remote_rules)
            return remote_rules

        # Fallback to local cache
        logger.warning(
            f"Using local cache fallback for scraping rules of engine '{engine_name}'."
        )
        cached_rules = self._read_rules_from_cache(engine_name)
        if cached_rules is not None:
            return cached_rules

        raise ConfigError(
            f"Failed to retrieve scraping rules for engine '{engine_name}': "
            "remote service unavailable and no valid local cache exists."
        )

    def _fetch_rules_from_remote(self, engine_name: str) -> Optional[Dict[str, Any]]:
        """Query Supabase REST endpoint for engine scraping rules."""
        if not self.is_configured:
            return None

        # 1. Via provided client object if available
        if self.supabase_client is not None and hasattr(self.supabase_client, "table"):
            try:
                res = (
                    self.supabase_client.table("hs_scraping_configs")
                    .select("*")
                    .eq("engine", engine_name)
                    .execute()
                )
                data = res.data if hasattr(res, "data") else res
                if data and isinstance(data, list):
                    item = data[0]
                    return item.get("rules") or item.get("config") or item
            except Exception as exc:
                logger.warning(f"Supabase client query for '{engine_name}' failed: {exc}")
                return None

        # 2. Via PostgREST direct endpoint
        if self.supabase_url and self.supabase_key:
            endpoint = f"{self.supabase_url}/rest/v1/hs_scraping_configs"
            params = {
                "engine": f"eq.{engine_name}",
                "select": "*",
            }
            headers = {
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "application/json",
            }
            try:
                response = requests.get(
                    endpoint,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list) and len(data) > 0:
                        item = data[0]
                        return item.get("rules") or item.get("config") or item
                else:
                    logger.warning(
                        f"Supabase returned status {response.status_code} for hs_scraping_configs."
                    )
            except Exception as exc:
                logger.warning(f"Network error querying hs_scraping_configs: {exc}")

        return None

    def _read_rules_from_cache(self, engine_name: str) -> Optional[Dict[str, Any]]:
        """Read scraping rules for an engine from config_cache.json."""
        cache_data = self._read_json_cache(self.config_cache_file)
        if isinstance(cache_data, dict):
            return cache_data.get(engine_name)
        return None

    def _update_rules_cache(self, engine_name: str, rules: Dict[str, Any]) -> None:
        """Update and persist rules for engine in config_cache.json."""
        current_cache = self._read_json_cache(self.config_cache_file)
        if not isinstance(current_cache, dict):
            current_cache = {}
        current_cache[engine_name] = rules
        self._write_json_cache(self.config_cache_file, current_cache)

    # -------------------------------------------------------------------------
    # Target Hotels
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_hotels(hotels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ensure optional hotel columns have safe defaults without breaking in-place identity."""
        for item in hotels:
            if isinstance(item, dict):
                raw_rules = item.get("matching_rules")
                if isinstance(raw_rules, str):
                    try:
                        parsed = json.loads(raw_rules)
                        item["matching_rules"] = parsed if isinstance(parsed, dict) else {}
                    except Exception:
                        item["matching_rules"] = {}
                elif not isinstance(raw_rules, dict):
                    item["matching_rules"] = {}
                if "rooms_catalog_url" not in item:
                    item["rooms_catalog_url"] = None
                if "booking_rooms_url" not in item:
                    item["booking_rooms_url"] = None
        return hotels

    def get_target_hotels(self) -> List[Dict[str, Any]]:
        """Fetch list of target hotels to scrape.

        Tries Supabase 'hs_target_hotels' table first. On failure, falls back
        to 'hotels_cache.json'. Raises ConfigError if both fail.

        Returns:
            List of hotel dicts with keys: id, name, booking_url, direct_url,
            direct_booking_engine, is_active, matching_rules, rooms_catalog_url,
            booking_rooms_url.
        """
        remote_hotels = self._fetch_hotels_from_remote()
        if remote_hotels is not None:
            self._write_json_cache(self.hotels_cache_file, remote_hotels)
            return remote_hotels

        # Fallback to local cache
        logger.warning("Using local cache fallback for target hotels list.")
        cached_hotels = self._read_json_cache(self.hotels_cache_file)
        if cached_hotels is not None and isinstance(cached_hotels, list):
            return self._normalize_hotels(cached_hotels)

        raise ConfigError(
            "Failed to retrieve target hotels: "
            "remote service unavailable and no valid local cache exists."
        )

    def _fetch_hotels_from_remote(self) -> Optional[List[Dict[str, Any]]]:
        """Query Supabase REST endpoint for hs_target_hotels."""
        if not self.is_configured:
            return None

        # 1. Via client object if available
        if self.supabase_client is not None and hasattr(self.supabase_client, "table"):
            try:
                res = (
                    self.supabase_client.table("hs_target_hotels")
                    .select("id, name, booking_url, direct_url, direct_booking_engine, is_active")
                    .eq("is_active", True)
                    .execute()
                )
                data = res.data if hasattr(res, "data") else res
                if isinstance(data, list):
                    return self._normalize_hotels(data)
            except Exception as exc:
                logger.warning(f"Supabase client query for hs_target_hotels failed: {exc}")
                return None

        # 2. Via PostgREST direct endpoint
        if self.supabase_url and self.supabase_key:
            endpoint = f"{self.supabase_url}/rest/v1/hs_target_hotels"
            params = {
                "select": "id,name,booking_url,direct_url,direct_booking_engine,is_active,matching_rules,rooms_catalog_url,booking_rooms_url",
                "is_active": "eq.true",
            }
            headers = {
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "application/json",
            }
            try:
                response = requests.get(
                    endpoint,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list):
                        return self._normalize_hotels(data)
                else:
                    logger.warning(
                        f"Supabase returned status {response.status_code} for hs_target_hotels."
                    )
            except Exception as exc:
                logger.warning(f"Network error querying hs_target_hotels: {exc}")

        return None

    # -------------------------------------------------------------------------
    # Room Mappings ('hs-room_mappings')
    # -------------------------------------------------------------------------

    @staticmethod
    def _sort_room_mappings(mappings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort room mappings by ascending priority."""
        return sorted(
            [m for m in mappings if isinstance(m, dict)],
            key=lambda m: m.get("priority") if isinstance(m.get("priority"), (int, float)) else 100,
        )

    def _fetch_room_mappings_from_remote(
        self, hotel_id: Optional[str] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """Query Supabase for active records in 'hs-room_mappings'."""
        if not self.is_configured:
            return None

        # 1. Via client object if available
        if self.supabase_client is not None and hasattr(self.supabase_client, "table"):
            try:
                query = (
                    self.supabase_client.table("hs-room_mappings")
                    .select("*")
                    .eq("is_active", True)
                )
                if hotel_id:
                    query = query.eq("hotel_id", hotel_id)
                data = None
                if hasattr(query, "order") and callable(query.order):
                    ordered = query.order("priority", desc=False)
                    if hasattr(ordered, "execute"):
                        res = ordered.execute()
                        data = res.data if hasattr(res, "data") else res
                if not isinstance(data, list) and hasattr(query, "execute"):
                    res = query.execute()
                    data = res.data if hasattr(res, "data") else res
                if isinstance(data, list):
                    return data
            except Exception as exc:
                logger.warning(f"Supabase client query for hs-room_mappings failed: {exc}")
                return None

        # 2. Via PostgREST direct endpoint
        if self.supabase_url and self.supabase_key:
            endpoint = f"{self.supabase_url}/rest/v1/hs-room_mappings"
            params: Dict[str, str] = {
                "select": "*",
                "is_active": "eq.true",
                "order": "priority.asc",
            }
            if hotel_id:
                params["hotel_id"] = f"eq.{hotel_id}"
            headers = {
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "application/json",
            }
            try:
                response = requests.get(
                    endpoint,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list):
                        return data
                else:
                    logger.warning(
                        f"Supabase returned status {response.status_code} for hs-room_mappings."
                    )
            except Exception as exc:
                logger.warning(f"Network error querying hs-room_mappings: {exc}")

        return None

    def get_room_mappings(self, hotel_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch active room mappings from 'hs-room_mappings' with local cache fallback."""
        remote_mappings = self._fetch_room_mappings_from_remote(hotel_id=hotel_id)
        if remote_mappings is not None:
            sorted_mappings = self._sort_room_mappings(remote_mappings)
            if hotel_id:
                existing_cache = self._read_json_cache(self.room_mappings_cache_file)
                if isinstance(existing_cache, list):
                    merged = [
                        m
                        for m in existing_cache
                        if isinstance(m, dict) and str(m.get("hotel_id", "")) != str(hotel_id)
                    ]
                    merged.extend(sorted_mappings)
                    self._write_json_cache(self.room_mappings_cache_file, merged)
                else:
                    self._write_json_cache(self.room_mappings_cache_file, sorted_mappings)
            else:
                self._write_json_cache(self.room_mappings_cache_file, sorted_mappings)
            return sorted_mappings

        logger.warning("Using local cache fallback for room mappings.")
        cached_mappings = self._read_json_cache(self.room_mappings_cache_file)
        if isinstance(cached_mappings, list):
            filtered = [
                m
                for m in cached_mappings
                if isinstance(m, dict)
                and m.get("is_active", True) is True
                and (not hotel_id or str(m.get("hotel_id", "")) == str(hotel_id))
            ]
            return self._sort_room_mappings(filtered)

        return []

    def upsert_room_mappings(self, mappings: List[Dict[str, Any]]) -> bool:
        """Upsert room mappings into 'hs-room_mappings', preserving is_manual_override records."""
        if not mappings:
            return True

        if not self.is_configured:
            logger.warning("Cannot upsert room mappings: Supabase is not configured.")
            return False

        # Retrieve existing mappings to protect is_manual_override == True records
        existing_records = self._fetch_room_mappings_from_remote()
        if not isinstance(existing_records, list):
            cached = self._read_json_cache(self.room_mappings_cache_file)
            existing_records = cached if isinstance(cached, list) else []

        manual_lookup: Dict[tuple, Dict[str, Any]] = {}
        for rec in existing_records:
            if isinstance(rec, dict) and rec.get("is_manual_override") is True:
                key = (
                    str(rec.get("hotel_id", "")),
                    str(rec.get("direct_room_pattern", "")).strip().lower(),
                )
                manual_lookup[key] = rec

        now_iso = datetime.utcnow().isoformat()
        prepared_list: List[Dict[str, Any]] = []
        for item in mappings:
            if not isinstance(item, dict):
                continue
            prepared = dict(item)
            prepared.setdefault("is_active", True)
            prepared["last_synced_at"] = now_iso

            key = (
                str(prepared.get("hotel_id", "")),
                str(prepared.get("direct_room_pattern", "")).strip().lower(),
            )
            existing_manual = manual_lookup.get(key)
            if existing_manual is not None:
                prepared["booking_room_name"] = existing_manual.get("booking_room_name")
                prepared["capacity_group"] = existing_manual.get("capacity_group")
                prepared["is_manual_override"] = True
                if "is_exclusive_direct" in existing_manual:
                    prepared["is_exclusive_direct"] = existing_manual["is_exclusive_direct"]
                if "match_score" in existing_manual:
                    prepared["match_score"] = existing_manual["match_score"]

            prepared_list.append(prepared)

        if not prepared_list:
            return True

        success = False

        # 1. Via Supabase client if available
        if self.supabase_client is not None and hasattr(self.supabase_client, "table"):
            try:
                (
                    self.supabase_client.table("hs-room_mappings")
                    .upsert(
                        prepared_list,
                        on_conflict="hotel_id,capacity_group,direct_room_pattern",
                    )
                    .execute()
                )
                success = True
            except Exception as exc:
                logger.warning(f"Supabase client upsert for hs-room_mappings failed: {exc}")
                return False

        # 2. Via PostgREST direct endpoint
        elif self.supabase_url and self.supabase_key:
            endpoint = f"{self.supabase_url}/rest/v1/hs-room_mappings"
            params = {"on_conflict": "hotel_id,capacity_group,direct_room_pattern"}
            headers = {
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=representation",
            }
            try:
                response = requests.post(
                    endpoint,
                    params=params,
                    headers=headers,
                    json=prepared_list,
                    timeout=self.timeout,
                )
                if response.status_code in (200, 201, 204):
                    success = True
                else:
                    logger.warning(
                        f"Supabase returned status {response.status_code} during upsert to hs-room_mappings: {response.text[:200]}"
                    )
                    return False
            except Exception as exc:
                logger.warning(f"Network error upserting hs-room_mappings: {exc}")
                return False

        if success:
            # Update local room_mappings_cache.json
            existing_cache = self._read_json_cache(self.room_mappings_cache_file)
            cache_map: Dict[tuple, Dict[str, Any]] = {}
            if isinstance(existing_cache, list):
                for rec in existing_cache:
                    if isinstance(rec, dict):
                        ck = (
                            str(rec.get("hotel_id", "")),
                            str(rec.get("capacity_group", "")),
                            str(rec.get("direct_room_pattern", "")).strip().lower(),
                        )
                        cache_map[ck] = rec
            for rec in prepared_list:
                ck = (
                    str(rec.get("hotel_id", "")),
                    str(rec.get("capacity_group", "")),
                    str(rec.get("direct_room_pattern", "")).strip().lower(),
                )
                cache_map[ck] = rec
            self._write_json_cache(
                self.room_mappings_cache_file,
                self._sort_room_mappings(list(cache_map.values())),
            )

        return success

    # -------------------------------------------------------------------------
    # Price Comparisons ('hs-price_comparisons')
    # -------------------------------------------------------------------------

    def upsert_price_comparisons(self, records: List[Dict[str, Any]]) -> int:
        """Batch-upsert price comparison records into 'hs-price_comparisons'."""
        if not records:
            return 0

        if not self.is_configured:
            logger.warning("Cannot upsert price comparisons: Supabase is not configured.")
            return 0

        prepared_list: List[Dict[str, Any]] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            prepared = dict(item)
            raw_room = prepared.get("room_name")
            prepared["room_name"] = "" if raw_room is None else str(raw_room).strip()

            raw_channel = prepared.get("channel")
            prepared["channel"] = "" if raw_channel is None else str(raw_channel).strip()

            raw_price = prepared.get("price")
            prepared["price"] = float(raw_price) if raw_price is not None else None

            raw_ages = prepared.get("children_ages")
            if isinstance(raw_ages, (list, tuple)):
                prepared["children_ages"] = ", ".join(str(a) for a in raw_ages)
            elif raw_ages is None:
                prepared["children_ages"] = ""
            else:
                prepared["children_ages"] = str(raw_ages)

            prepared_list.append(prepared)

        if not prepared_list:
            return 0

        if self.supabase_client is not None and hasattr(self.supabase_client, "table"):
            try:
                res = (
                    self.supabase_client.table("hs-price_comparisons")
                    .upsert(prepared_list, on_conflict=PRICE_COMPARISONS_CONFLICT_COLS)
                    .execute()
                )
                data = res.data if hasattr(res, "data") else res
                if isinstance(data, list) and len(data) > 0:
                    return len(data)
                return len(prepared_list)
            except Exception as exc:
                logger.warning(f"Supabase client upsert for hs-price_comparisons failed: {exc}")
                return 0

        if self.supabase_url and self.supabase_key:
            endpoint = f"{self.supabase_url}/rest/v1/hs-price_comparisons"
            params = {"on_conflict": PRICE_COMPARISONS_CONFLICT_COLS}
            headers = {
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=representation",
            }
            try:
                response = requests.post(
                    endpoint,
                    params=params,
                    headers=headers,
                    json=prepared_list,
                    timeout=self.timeout,
                )
                if response.status_code in (200, 201, 204):
                    try:
                        data = response.json()
                        if isinstance(data, list) and len(data) > 0:
                            return len(data)
                    except Exception:
                        pass
                    return len(prepared_list)
                logger.warning(
                    f"Supabase returned status {response.status_code} during upsert to hs-price_comparisons: {response.text[:200]}"
                )
                return 0
            except Exception as exc:
                logger.warning(f"Network error upserting hs-price_comparisons: {exc}")
                return 0

        return 0

    @staticmethod
    def _normalize_price_comparisons(
        rows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Normalize optional list fields in fetched price comparison rows."""
        for item in rows:
            if isinstance(item, dict):
                raw_ages = item.get("children_ages")
                if isinstance(raw_ages, (list, tuple)):
                    item["children_ages"] = ", ".join(str(a) for a in raw_ages)
        return rows

    def fetch_all_price_comparisons(self) -> List[Dict[str, Any]]:
        """Fetch all price comparison records from 'hs-price_comparisons'."""
        if not self.is_configured:
            logger.warning("Cannot fetch price comparisons: Supabase is not configured.")
            return []

        if self.supabase_client is not None and hasattr(self.supabase_client, "table"):
            try:
                query = self.supabase_client.table("hs-price_comparisons").select("*")
                res = query.execute()
                data = res.data if hasattr(res, "data") else res
                if isinstance(data, list):
                    return self._normalize_price_comparisons(data)
                return []
            except Exception as exc:
                logger.warning(f"Supabase client query for hs-price_comparisons failed: {exc}")
                return []

        if self.supabase_url and self.supabase_key:
            endpoint = f"{self.supabase_url}/rest/v1/hs-price_comparisons"
            params = {
                "select": "*",
                "order": (
                    "scraped_date.desc,hotel_name.asc,check_in.asc,"
                    "capacity_group.asc,matched_room_key.asc,channel.asc,price.asc"
                ),
            }
            headers = {
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "application/json",
            }
            try:
                response = requests.get(
                    endpoint,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list):
                        return self._normalize_price_comparisons(data)
                    return []
                logger.warning(
                    f"Supabase returned status {response.status_code} for hs-price_comparisons."
                )
            except Exception as exc:
                logger.warning(f"Network error querying hs-price_comparisons: {exc}")

        return []


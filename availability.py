"""
availability.py
---------------
Queries recreation.gov's internal availability API (the same JSON endpoint
the site's React frontend uses). This is much faster than rendering HTML.

Endpoint patterns:
  Monthly availability:
    GET https://www.recreation.gov/api/camps/availability/campground/{id}/month
        ?start_date=YYYY-MM-01T00:00:00.000Z

  Campsite detail (loop name + attributes like Max Vehicle Length):
    GET https://www.recreation.gov/api/camps/campsites/{campsite_id}

Availability response shape (simplified):
  {
    "campsites": {
      "<campsite_id>": {
        "site": "<site_name>",
        "loop": "<loop_name>",
        "availabilities": {
          "YYYY-MM-DDTHH:MM:SSZ": "Available" | "Reserved" | "NotAvailable" | ...
        }
      }
    }
  }

Campsite detail response shape (simplified):
  {
    "campsite": {
      "campsite_id": "...",
      "site": "001",
      "loop": "Loop C",
      "attributes": [
        {"attribute_name": "Max Vehicle Length", "attribute_value": "35"},
        ...
      ]
    }
  }
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Base URL for the internal availability API
AVAILABILITY_API = (
    "https://www.recreation.gov/api/camps/availability/campground"
    "/{campground_id}/month"
)

# Per-campsite detail endpoint (returns loop name + attributes)
CAMPSITE_DETAIL_API = (
    "https://www.recreation.gov/api/camps/campsites/{campsite_id}"
)

# The single availability status string that means "open for booking"
AVAILABLE_STATUS = "Available"

# Headers that mimic a real browser — reduces chance of getting rate-limited
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.recreation.gov/",
    "Origin": "https://www.recreation.gov",
}


class AvailabilityChecker:
    """
    Checks campsite availability via recreation.gov's internal JSON API.

    Typical usage
    -------------
    checker = AvailabilityChecker(campground_id="233687")

    # One-time preflight: find all sites on Loop C that fit a 30-ft camper.
    eligible = checker.build_eligible_sites(loop_filter="C", min_vehicle_length_ft=30)

    # Tight polling loop at release time:
    available = checker.find_available_sites(
        checkin=date(2025, 6, 1),
        checkout=date(2025, 6, 3),
        eligible_sites=eligible,
    )
    # Returns list of (campsite_id, site_name) tuples that are fully open.
    """

    def __init__(self, campground_id: str, session: Optional[requests.Session] = None):
        self.campground_id = campground_id
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

        # Cache of raw API responses keyed by "YYYY-MM" to avoid redundant calls
        self._cache: dict[str, dict] = {}

        # Map from site name/number -> campsite_id, populated on first fetch
        self._site_name_to_id: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def build_eligible_sites(
        self,
        loop_filter: str = "",
        min_vehicle_length_ft: int = 0,
        reference_date: Optional[date] = None,
    ) -> list[tuple[str, str]]:
        """
        Fetch campsite attributes and return (campsite_id, site_name) for
        every site that passes the loop and vehicle-length filters.

        This is meant to be called ONCE during the pre-flight window
        (a couple of minutes before 10 AM) so the result is ready when
        polling begins.  It makes one availability call to get the site list,
        then fans out per-site detail calls in parallel.

        Parameters
        ----------
        loop_filter : str
            Case-insensitive substring that must appear in the site's loop
            name.  E.g. "C" matches "Loop C", "LOOP C", "loop c".
            Pass "" to accept all loops.
        min_vehicle_length_ft : int
            Minimum value of the "Max Vehicle Length" attribute (feet).
            Pass 0 to skip this filter.
        reference_date : date, optional
            Month to query for the site list. Defaults to today.
        """
        ref = reference_date or date.today()
        month_data = self._fetch_month(ref.replace(day=1))
        if not month_data:
            logger.error("Could not fetch site list for %s", ref)
            return []

        self._build_site_name_index(month_data)

        # --- Step 1: coarse filter by loop field in availability response ---
        loop_filter_upper = loop_filter.upper()
        candidates: list[tuple[str, str]] = []  # (campsite_id, site_name)
        for campsite_id, site_data in month_data.items():
            if loop_filter_upper:
                loop_name = site_data.get("loop", "").upper()
                if loop_filter_upper not in loop_name:
                    continue
            candidates.append((campsite_id, site_data.get("site", campsite_id)))

        logger.info(
            "Loop filter '%s' matched %d site(s).", loop_filter, len(candidates)
        )

        if not candidates:
            return []

        # --- Step 2: fetch per-site attributes (parallel) for length filter ---
        if min_vehicle_length_ft <= 0:
            # No length filter — skip the attribute API calls
            eligible = sorted(candidates, key=lambda t: self._sort_key(t[1]))
            logger.info(
                "No vehicle-length filter — %d eligible site(s).", len(eligible)
            )
            return eligible

        logger.info(
            "Fetching attributes for %d site(s) to check vehicle length >= %d ft ...",
            len(candidates),
            min_vehicle_length_ft,
        )

        eligible: list[tuple[str, str]] = []
        # Fan out attribute requests in parallel (up to 10 at a time)
        with ThreadPoolExecutor(max_workers=10) as pool:
            future_to_site = {
                pool.submit(self._fetch_campsite_detail, cid): (cid, name)
                for cid, name in candidates
            }
            for future in as_completed(future_to_site):
                campsite_id, site_name = future_to_site[future]
                detail = future.result()
                if detail is None:
                    logger.warning(
                        "Could not fetch detail for site %s — including it anyway.",
                        site_name,
                    )
                    eligible.append((campsite_id, site_name))
                    continue

                max_len = self._get_max_vehicle_length(detail)
                if max_len is None:
                    # No length attribute recorded — include the site
                    logger.debug(
                        "Site %s has no Max Vehicle Length attribute — including it.",
                        site_name,
                    )
                    eligible.append((campsite_id, site_name))
                elif max_len >= min_vehicle_length_ft:
                    logger.debug(
                        "Site %s: max vehicle length %d ft — ELIGIBLE.", site_name, max_len
                    )
                    eligible.append((campsite_id, site_name))
                else:
                    logger.debug(
                        "Site %s: max vehicle length %d ft < %d ft — skipping.",
                        site_name,
                        max_len,
                        min_vehicle_length_ft,
                    )

        eligible.sort(key=lambda t: self._sort_key(t[1]))
        logger.info(
            "%d site(s) eligible after vehicle-length filter (>= %d ft): %s",
            len(eligible),
            min_vehicle_length_ft,
            [name for _, name in eligible],
        )
        return eligible

    def find_available_sites(
        self,
        checkin: date,
        checkout: date,
        eligible_sites: Optional[list[tuple[str, str]]] = None,
    ) -> list[tuple[str, str]]:
        """
        Return a list of (campsite_id, site_name) for every eligible site
        that is 'Available' for the full checkin->checkout range.

        Parameters
        ----------
        eligible_sites : list of (campsite_id, site_name), optional
            Pre-filtered site list from build_eligible_sites().  When
            provided, only these sites are checked — no loop or length
            filtering is re-applied here.  When omitted, all sites in
            the campground are checked (no filtering).
        """
        months_needed = self._months_in_range(checkin, checkout)

        all_site_data: dict[str, dict] = {}
        for month_start in months_needed:
            month_data = self._fetch_month(month_start)
            if month_data:
                all_site_data.update(month_data)

        if not all_site_data:
            logger.warning("No site data returned from availability API.")
            return []

        self._build_site_name_index(all_site_data)

        # Use the pre-filtered list if provided; otherwise consider everything
        if eligible_sites is not None:
            candidate_ids = eligible_sites
        else:
            candidate_ids = [
                (cid, data.get("site", cid))
                for cid, data in all_site_data.items()
            ]

        nights = self._nights_in_range(checkin, checkout)
        available = []
        for campsite_id, site_name in candidate_ids:
            site_data = all_site_data.get(campsite_id)
            if site_data and self._is_fully_available(site_data, nights):
                available.append((campsite_id, site_name))

        return available

    def clear_cache(self):
        """Clear cached month data so the next call fetches fresh results."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_month(self, month_start: date) -> Optional[dict]:
        """
        Fetch (and cache) availability data for the month containing month_start.
        Returns the dict of {campsite_id: site_data} or None on error.
        """
        cache_key = month_start.strftime("%Y-%m")
        if cache_key in self._cache:
            return self._cache[cache_key]

        # API requires the first day of the month in ISO-8601
        start_str = month_start.strftime("%Y-%m-01T00:00:00.000Z")
        url = AVAILABILITY_API.format(campground_id=self.campground_id)
        params = {"start_date": start_str}

        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            campsites = data.get("campsites", {})
            self._cache[cache_key] = campsites
            logger.debug(
                "Fetched %d sites for %s", len(campsites), cache_key
            )
            return campsites
        except requests.RequestException as exc:
            logger.error("API request failed for %s: %s", cache_key, exc)
            return None

    def _build_site_name_index(self, all_site_data: dict):
        """Populate self._site_name_to_id from API response data."""
        for campsite_id, site_data in all_site_data.items():
            site_name = site_data.get("site", "")
            if site_name:
                # Store both the full name and any trailing number
                self._site_name_to_id[site_name] = campsite_id
                # Also index by the numeric portion if the name is like "001"
                stripped = site_name.lstrip("0") or "0"
                self._site_name_to_id[stripped] = campsite_id

    def _fetch_campsite_detail(self, campsite_id: str) -> Optional[dict]:
        """
        Fetch the detail record for a single campsite.
        Returns the inner "campsite" dict, or None on error.
        """
        url = CAMPSITE_DETAIL_API.format(campsite_id=campsite_id)
        try:
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json().get("campsite", {})
        except requests.RequestException as exc:
            logger.warning("Detail fetch failed for campsite %s: %s", campsite_id, exc)
            return None

    @staticmethod
    def _get_max_vehicle_length(campsite_detail: dict) -> Optional[int]:
        """
        Extract the 'Max Vehicle Length' attribute value (as int feet) from
        a campsite detail dict.  Returns None if the attribute is absent or
        cannot be parsed.
        """
        for attr in campsite_detail.get("attributes", []):
            name = attr.get("attribute_name", "").lower()
            if "max vehicle length" in name or "vehicle length" in name:
                try:
                    return int(float(attr.get("attribute_value", "")))
                except (ValueError, TypeError):
                    return None
        return None

    @staticmethod
    def _sort_key(site_name: str) -> tuple:
        """Sort site names numerically when possible (001 < 002 < 10 < 34)."""
        try:
            return (0, int(site_name.lstrip("0") or "0"))
        except ValueError:
            return (1, site_name)

    def _is_fully_available(self, site_data: dict, nights: list[date]) -> bool:
        """Return True only if every night in `nights` is 'Available'."""
        availabilities = site_data.get("availabilities", {})
        for night in nights:
            # API keys look like "2025-06-01T00:00:00Z"
            key = night.strftime("%Y-%m-%dT00:00:00Z")
            if availabilities.get(key) != AVAILABLE_STATUS:
                return False
        return True

    @staticmethod
    def _nights_in_range(checkin: date, checkout: date) -> list[date]:
        """Return every night from checkin up to (but not including) checkout."""
        nights = []
        current = checkin
        while current < checkout:
            nights.append(current)
            current += timedelta(days=1)
        return nights

    @staticmethod
    def _months_in_range(checkin: date, checkout: date) -> list[date]:
        """Return the first-of-month dates for every month touched by the range."""
        months = []
        current = checkin.replace(day=1)
        end = checkout.replace(day=1)
        while current <= end:
            months.append(current)
            # Advance to next month
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)
        return months

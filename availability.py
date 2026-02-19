"""
availability.py
---------------
Queries recreation.gov's internal availability API (the same JSON endpoint
the site's React frontend uses). This is much faster than rendering HTML.

Endpoint pattern:
  GET https://www.recreation.gov/api/camps/availability/campground/{id}/month
      ?start_date=YYYY-MM-01T00:00:00.000Z

Response shape (simplified):
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
"""

import logging
from datetime import date, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Base URL for the internal availability API
AVAILABILITY_API = (
    "https://www.recreation.gov/api/camps/availability/campground"
    "/{campground_id}/month"
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

    Usage
    -----
    checker = AvailabilityChecker(campground_id="233687")
    available = checker.find_available_sites(
        checkin=date(2025, 6, 1),
        checkout=date(2025, 6, 3),
        preferred_sites=["1", "2", "3", ...],  # site names / numbers
    )
    # Returns list of (campsite_id, site_name) tuples that are fully open
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

    def find_available_sites(
        self,
        checkin: date,
        checkout: date,
        preferred_sites: Optional[list[str]] = None,
    ) -> list[tuple[str, str]]:
        """
        Return a list of (campsite_id, site_name) for every site that is
        'Available' for the full checkin->checkout range.

        If preferred_sites is given, only those sites are considered.
        Results are ordered by preferred_sites order when possible.
        """
        # Gather all months we need to query (checkin might span a month boundary)
        months_needed = self._months_in_range(checkin, checkout)

        # Fetch/cache API data for each needed month
        all_site_data: dict[str, dict] = {}
        for month_start in months_needed:
            month_data = self._fetch_month(month_start)
            if month_data:
                all_site_data.update(month_data)

        if not all_site_data:
            logger.warning("No site data returned from availability API.")
            return []

        # Build site_name -> campsite_id lookup from fresh data
        self._build_site_name_index(all_site_data)

        # Determine which campsite_ids to check
        if preferred_sites:
            candidate_ids = [
                (self._site_name_to_id[s], s)
                for s in preferred_sites
                if s in self._site_name_to_id
            ]
            if len(candidate_ids) < len(preferred_sites):
                missing = [
                    s for s in preferred_sites if s not in self._site_name_to_id
                ]
                logger.debug("Could not find campsite IDs for sites: %s", missing)
        else:
            candidate_ids = [
                (cid, data.get("site", cid))
                for cid, data in all_site_data.items()
            ]

        # Filter to sites that are Available for every night of the stay
        available = []
        nights = self._nights_in_range(checkin, checkout)

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

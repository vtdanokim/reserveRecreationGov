"""
main.py
-------
Orchestrator for the Goose Point Campground booking bot.

Flow
----
1.  Load config.yaml
2.  Sync clock with NTP
3.  Show countdown until release
4.  At T-2 min: launch browser, log in, pre-load campground page
5.  At T-2 sec: start polling the availability API every 0.3 s
6.  As soon as an available site appears: trigger Playwright to add to cart
7.  Walk the checkout flow up to (but not including) the final confirm button
8.  Alert the user to finish the booking manually in the open browser window

Run with:
    python main.py                         # uses config.yaml in current dir
    python main.py --config /path/to/cfg   # custom config path
    python main.py --check-only            # just print current availability and exit
    python main.py --dry-run               # simulate the full flow without booking
"""

import argparse
import asyncio
import logging
import sys
import time
from datetime import date
from pathlib import Path

import yaml

from availability import AvailabilityChecker
from booker import Booker
from timer import ReleaseTimer

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path.resolve())
        sys.exit(1)
    with config_path.open() as fh:
        cfg = yaml.safe_load(fh)
    return cfg


# ---------------------------------------------------------------------------
# Availability check helper (synchronous, for polling loop)
# ---------------------------------------------------------------------------

def check_availability(checker: AvailabilityChecker, cfg: dict) -> list[tuple[str, str]]:
    """
    Poll the availability API once and return a list of (campsite_id, site_name)
    tuples for every preferred site that is currently available.
    """
    checkin = date.fromisoformat(cfg["booking"]["checkin_date"])
    checkout = date.fromisoformat(cfg["booking"]["checkout_date"])
    preferred = cfg["booking"].get("preferred_sites", [])

    checker.clear_cache()  # Always fetch fresh data in polling mode
    return checker.find_available_sites(checkin, checkout, preferred)


# ---------------------------------------------------------------------------
# Check-only mode
# ---------------------------------------------------------------------------

def run_check_only(cfg: dict):
    """Print current availability and exit — no booking attempted."""
    campground_id = cfg["recreation_gov"]["campground_id"]
    checker = AvailabilityChecker(campground_id)
    available = check_availability(checker, cfg)

    checkin = cfg["booking"]["checkin_date"]
    checkout = cfg["booking"]["checkout_date"]
    print(f"\nCampground {campground_id} — {checkin} to {checkout}")
    if available:
        print(f"  {len(available)} available site(s):")
        for campsite_id, site_name in available:
            print(f"    Site {site_name:>4}  (internal ID: {campsite_id})")
    else:
        print("  No preferred sites are currently available.")
    print()


# ---------------------------------------------------------------------------
# Main async booking flow
# ---------------------------------------------------------------------------

async def run_booking(cfg: dict, dry_run: bool = False):
    timing = cfg.get("timing", {})
    release_hour   = timing.get("release_hour", 10)
    release_minute = timing.get("release_minute", 0)
    release_second = timing.get("release_second", 0)
    tz_name        = timing.get("timezone", "America/New_York")
    preflight_secs = timing.get("preflight_seconds", 120)
    poll_start_secs = timing.get("poll_start_seconds", 2)
    poll_interval  = timing.get("poll_interval_seconds", 0.3)
    poll_timeout   = timing.get("poll_timeout_seconds", 60)

    campground_id = cfg["recreation_gov"]["campground_id"]
    checkin  = date.fromisoformat(cfg["booking"]["checkin_date"])
    checkout = date.fromisoformat(cfg["booking"]["checkout_date"])

    # -----------------------------------------------------------------------
    # Step 1: NTP sync
    # -----------------------------------------------------------------------
    timer = ReleaseTimer(
        release_hour=release_hour,
        release_minute=release_minute,
        release_second=release_second,
        tz_name=tz_name,
    )
    timer.sync()

    release_utc = timer.next_release_utc()
    logger.info(
        "Release time: %s ET  (%s UTC)",
        release_utc.astimezone(__import__("zoneinfo").ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S"),
        release_utc.strftime("%Y-%m-%d %H:%M:%S"),
    )
    logger.info("Booking target: %s to %s", checkin, checkout)
    logger.info("Preferred sites: %s", cfg["booking"].get("preferred_sites", []))
    logger.info(timer.status_line())

    # -----------------------------------------------------------------------
    # Step 2: Wait for pre-flight window, then launch browser
    # -----------------------------------------------------------------------
    timer.wait_until_preflight(preflight_secs)
    logger.info("Pre-flight window reached — launching browser ...")

    async with Booker(cfg) as booker:
        if not dry_run:
            logged_in = await booker.login()
            if not logged_in:
                logger.error("Aborting — could not log in.")
                return
            await booker.preload_campground()
        else:
            logger.info("[DRY RUN] Skipping login and page pre-load.")

        # -------------------------------------------------------------------
        # Step 3: Wait until T-poll_start_secs, then start API polling
        # -------------------------------------------------------------------
        timer.wait_until_poll_start(poll_start_secs)
        logger.info(
            "Starting availability polling (interval=%.2fs, timeout=%.0fs) ...",
            poll_interval,
            poll_timeout,
        )

        checker = AvailabilityChecker(campground_id)
        booked = False
        poll_start = time.monotonic()

        while (time.monotonic() - poll_start) < poll_timeout:
            available = check_availability(checker, cfg)

            if available:
                campsite_id, site_name = available[0]
                logger.info(
                    "SITE AVAILABLE: %s (ID %s)", site_name, campsite_id
                )

                if dry_run:
                    logger.info("[DRY RUN] Would book site %s now.", site_name)
                    booked = True
                    break

                booked = await booker.book_site(
                    campsite_id=campsite_id,
                    site_name=site_name,
                    checkin=checkin,
                    checkout=checkout,
                )
                if booked:
                    break
                else:
                    logger.warning(
                        "Booking attempt for site %s failed — trying next available ...",
                        site_name,
                    )
                    # Remove the failed site from the preferred list and retry
                    preferred = cfg["booking"].get("preferred_sites", [])
                    if site_name in preferred:
                        preferred.remove(site_name)

            else:
                logger.debug("No available sites yet — polling again ...")

            time.sleep(poll_interval)

        if not booked:
            logger.warning(
                "Poll window expired without finding an available site. "
                "You may want to try again for cancellations."
            )

        # Keep the browser open so the user can finish the reservation
        if not dry_run and booked:
            input("\nPress Enter to close the browser when you're done ...\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Goose Point Campground reservation bot for recreation.gov"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Print current availability and exit without booking",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full timing/polling flow but skip actual browser booking",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.check_only:
        run_check_only(cfg)
        return

    asyncio.run(run_booking(cfg, dry_run=args.dry_run))


if __name__ == "__main__":
    main()

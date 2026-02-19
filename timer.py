"""
timer.py
--------
NTP-synced precision timer.

recreation.gov releases sites at exactly 10:00:00 AM ET.  Your local system
clock can easily be off by several seconds — enough to miss the window.  This
module:

  1. Queries a pool of NTP servers to measure the offset between your system
     clock and true UTC.
  2. Applies that offset when sleeping so that the bot wakes up at the
     correct real-world time, regardless of system clock drift.
"""

import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import ntplib

logger = logging.getLogger(__name__)

# NTP servers to try in order; we pick the first that responds
NTP_SERVERS = [
    "pool.ntp.org",
    "time.google.com",
    "time.cloudflare.com",
    "time.windows.com",
]


def _get_ntp_offset() -> float:
    """
    Return the clock offset (seconds) between this machine and NTP time.
    A positive offset means the system clock is *ahead* of real time.
    Returns 0.0 if all NTP servers fail (safe fallback).
    """
    client = ntplib.NTPClient()
    for server in NTP_SERVERS:
        try:
            response = client.request(server, version=3, timeout=5)
            offset = response.offset
            logger.info(
                "NTP sync via %s: offset = %.4f s (system clock is %s real time)",
                server,
                abs(offset),
                "ahead of" if offset > 0 else "behind",
            )
            return offset
        except ntplib.NTPException as exc:
            logger.warning("NTP server %s failed: %s", server, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("NTP server %s unreachable: %s", server, exc)

    logger.warning(
        "All NTP servers failed — using system clock (may be inaccurate)."
    )
    return 0.0


class ReleaseTimer:
    """
    Waits until a given release time using NTP-corrected system time.

    Parameters
    ----------
    release_hour, release_minute, release_second : int
        The local time of day when reservations open.
    tz_name : str
        IANA timezone name, e.g. "America/New_York".
    """

    def __init__(
        self,
        release_hour: int = 10,
        release_minute: int = 0,
        release_second: int = 0,
        tz_name: str = "America/New_York",
    ):
        self.release_hour = release_hour
        self.release_minute = release_minute
        self.release_second = release_second
        self.tz = ZoneInfo(tz_name)
        self.ntp_offset: float = 0.0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def sync(self):
        """Measure NTP offset. Call once at startup."""
        self.ntp_offset = _get_ntp_offset()

    def next_release_utc(self) -> datetime:
        """
        Return the next upcoming release moment as a UTC-aware datetime.
        If today's release time has already passed, returns tomorrow's.
        """
        now_local = self._now_local()
        release_today = now_local.replace(
            hour=self.release_hour,
            minute=self.release_minute,
            second=self.release_second,
            microsecond=0,
        )
        if now_local >= release_today:
            # Already past today's release — target tomorrow
            from datetime import timedelta
            release_today += timedelta(days=1)
        return release_today.astimezone(timezone.utc)

    def seconds_until_release(self) -> float:
        """
        How many seconds (NTP-corrected) until the next release moment.
        Negative if we're already past it.
        """
        release_utc = self.next_release_utc()
        now_utc = self._corrected_utc_now()
        delta = (release_utc - now_utc).total_seconds()
        return delta

    def wait_until(self, target_utc: datetime, poll_interval: float = 0.05):
        """
        Block until NTP-corrected time reaches target_utc.
        Uses a busy-wait with short sleeps for sub-second precision.
        """
        while True:
            remaining = (target_utc - self._corrected_utc_now()).total_seconds()
            if remaining <= 0:
                return
            # Sleep in chunks: long sleep far out, tight loop near the moment
            if remaining > 10:
                time.sleep(remaining - 10)
            elif remaining > 1:
                time.sleep(0.5)
            else:
                time.sleep(min(poll_interval, remaining / 2))

    def wait_until_preflight(self, preflight_seconds: float = 120):
        """
        Block until `preflight_seconds` before the release moment — i.e. the
        time to start logging in and loading pages.
        """
        from datetime import timedelta
        release_utc = self.next_release_utc()
        preflight_utc = release_utc - timedelta(seconds=preflight_seconds)

        remaining = (preflight_utc - self._corrected_utc_now()).total_seconds()
        if remaining > 0:
            logger.info(
                "Waiting %.0f s until pre-flight window (%s ET) ...",
                remaining,
                preflight_utc.astimezone(self.tz).strftime("%H:%M:%S"),
            )
            self.wait_until(preflight_utc)

    def wait_until_poll_start(self, poll_start_seconds: float = 2):
        """
        Block until `poll_start_seconds` before release — when we start
        hammering the availability API.
        """
        from datetime import timedelta
        release_utc = self.next_release_utc()
        poll_start_utc = release_utc - timedelta(seconds=poll_start_seconds)

        remaining = (poll_start_utc - self._corrected_utc_now()).total_seconds()
        if remaining > 0:
            logger.info(
                "Waiting %.1f s until API polling begins ...", remaining
            )
            self.wait_until(poll_start_utc)

    def status_line(self) -> str:
        """Return a human-readable countdown string."""
        secs = self.seconds_until_release()
        if secs < 0:
            return f"PAST release by {abs(secs):.2f}s"
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        s = secs % 60
        return f"Release in {h:02d}:{m:02d}:{s:05.2f}"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _corrected_utc_now(self) -> datetime:
        """Return the current UTC time adjusted for NTP offset."""
        raw = datetime.now(timezone.utc)
        # NTP offset: positive means system clock is ahead of real time,
        # so subtract to get real time.
        from datetime import timedelta
        return raw - timedelta(seconds=self.ntp_offset)

    def _now_local(self) -> datetime:
        """Return the NTP-corrected current time in the configured timezone."""
        return self._corrected_utc_now().astimezone(self.tz)

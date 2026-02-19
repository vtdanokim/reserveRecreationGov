"""
booker.py
---------
Playwright-based browser automation for recreation.gov.

Responsibilities:
  1. Log in to recreation.gov and hold an authenticated session.
  2. Pre-load the campground availability page so it's ready at release time.
  3. When the API reports an available site, navigate directly to that site's
     booking URL, add it to cart, and walk through checkout.
  4. Pause at the final "Place Order" / "Reserve" step so the human can
     review and confirm — we do NOT click the final button automatically
     to stay within ToS spirit and to avoid accidental double-charges.

Recreation.gov URL patterns
----------------------------
  Campground page:
    https://www.recreation.gov/camping/campgrounds/{campground_id}

  Single site availability:
    https://www.recreation.gov/camping/campgrounds/{campground_id}/r/campsiteDetails
        ?contractCode=NRSO
        &parkId={campground_id}
        &siteId={campsite_id}
        &availStartDate={YYYY-MM-DD}    <- checkin
        &inSeasonOnly=false
        &lengthOfStay={nights}

  Add-to-cart is triggered by clicking the "Add to Cart" button on that page.
"""

import asyncio
import logging
from datetime import date, timedelta
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.recreation.gov/log-in"
CAMPGROUND_URL = "https://www.recreation.gov/camping/campgrounds/{campground_id}"
SITE_DETAIL_URL = (
    "https://www.recreation.gov/camping/campgrounds/{campground_id}"
    "/r/campsiteDetails"
    "?contractCode=NRSO"
    "&parkId={campground_id}"
    "&siteId={campsite_id}"
    "&availStartDate={checkin}"
    "&inSeasonOnly=false"
    "&lengthOfStay={nights}"
)


class Booker:
    """
    Manages a Playwright browser session and handles the booking flow.

    Usage (async)
    -------------
    async with Booker(config) as booker:
        await booker.login()
        await booker.preload_campground()
        # ... wait for release ...
        success = await booker.book_site(campsite_id="12345", site_name="001")
    """

    def __init__(self, config: dict):
        self.cfg = config
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        browser_type_name = self.cfg.get("browser", {}).get("browser_type", "chromium")
        browser_type = getattr(self._playwright, browser_type_name)
        self._browser = await browser_type.launch(
            headless=self.cfg.get("browser", {}).get("headless", False),
            slow_mo=self.cfg.get("browser", {}).get("slow_mo", 0),
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        self._page = await self._context.new_page()
        return self

    async def __aexit__(self, *_):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def login(self) -> bool:
        """
        Navigate to the login page and authenticate.
        Returns True on success, False on failure.
        """
        email = self.cfg["recreation_gov"]["email"]
        password = self.cfg["recreation_gov"]["password"]

        logger.info("Logging in to recreation.gov ...")
        page = self._page

        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

        # Fill the login form
        try:
            await page.fill('input[name="email"], input[type="email"]', email)
            await page.fill('input[name="password"], input[type="password"]', password)
            await page.click('button[type="submit"]')

            # Wait for redirect away from the login page
            await page.wait_for_url(
                lambda url: "log-in" not in url, timeout=20_000
            )
            logger.info("Login successful.")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Login failed: %s", exc)
            await page.screenshot(path="login_failure.png")
            return False

    async def preload_campground(self):
        """
        Load the campground page so it's warm in the browser cache.
        Also pre-warms the session cookies.
        """
        campground_id = self.cfg["recreation_gov"]["campground_id"]
        url = CAMPGROUND_URL.format(campground_id=campground_id)
        logger.info("Pre-loading campground page: %s", url)
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        logger.info("Campground page pre-loaded.")

    async def book_site(
        self,
        campsite_id: str,
        site_name: str,
        checkin: date,
        checkout: date,
    ) -> bool:
        """
        Navigate directly to the campsite detail/booking page and add to cart.

        The bot WILL NOT click the final "Place Order" button — it pauses so
        the user can review and confirm the reservation manually.

        Returns True if the item was successfully added to cart.
        """
        campground_id = self.cfg["recreation_gov"]["campground_id"]
        nights = (checkout - checkin).days
        url = SITE_DETAIL_URL.format(
            campground_id=campground_id,
            campsite_id=campsite_id,
            checkin=checkin.strftime("%Y-%m-%d"),
            nights=nights,
        )

        logger.info("Navigating to site %s booking page ...", site_name)
        page = self._page

        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)

        # Look for the "Add to Cart" button (text may vary slightly)
        add_to_cart_selectors = [
            'button:has-text("Add to Cart")',
            'button:has-text("Add To Cart")',
            '[data-testid="add-to-cart-button"]',
        ]

        for selector in add_to_cart_selectors:
            try:
                btn = page.locator(selector).first
                await btn.wait_for(state="visible", timeout=8_000)
                logger.info("Clicking 'Add to Cart' for site %s ...", site_name)
                await btn.click()
                logger.info("'Add to Cart' clicked — navigating to checkout ...")
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            logger.error(
                "Could not find 'Add to Cart' button for site %s. "
                "Check login_failure.png / screenshot.png.",
                site_name,
            )
            await page.screenshot(path=f"add_to_cart_failure_{site_name}.png")
            return False

        # Navigate to cart / checkout
        try:
            await self._go_to_checkout(page)
            self._alert_user(site_name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Error during checkout navigation: %s", exc)
            await page.screenshot(path=f"checkout_failure_{site_name}.png")
            return False

    async def screenshot(self, path: str = "debug.png"):
        """Save a screenshot of the current page for debugging."""
        await self._page.screenshot(path=path)
        logger.info("Screenshot saved to %s", path)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _go_to_checkout(self, page: Page):
        """
        Navigate from the cart to the checkout page.
        Stops BEFORE the final confirmation / payment button.
        """
        # Try clicking a "Proceed to Checkout" style button
        checkout_selectors = [
            'button:has-text("Proceed to Checkout")',
            'a:has-text("Proceed to Checkout")',
            'button:has-text("Checkout")',
            '[data-testid="proceed-to-checkout"]',
        ]

        for selector in checkout_selectors:
            try:
                btn = page.locator(selector).first
                await btn.wait_for(state="visible", timeout=5_000)
                await btn.click()
                logger.info("Navigating through checkout flow ...")
                # Give the next page time to load
                await page.wait_for_load_state("networkidle", timeout=15_000)
                break
            except Exception:  # noqa: BLE001
                continue

        logger.info(
            "\n"
            "========================================================\n"
            "  SITE ADDED TO CART — ACTION REQUIRED\n"
            "  Review the details in the browser window and click\n"
            "  the final 'Reserve' / 'Place Order' button to confirm.\n"
            "========================================================\n"
        )

    @staticmethod
    def _alert_user(site_name: str):
        """Print a loud terminal alert and optionally beep."""
        print("\a")  # Terminal bell
        print("\n" + "=" * 60)
        print(f"  *** SITE {site_name} IS IN YOUR CART! ***")
        print("  Go to the browser and complete the reservation NOW.")
        print("=" * 60 + "\n")

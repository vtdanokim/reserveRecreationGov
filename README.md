# Goose Point Campground Booking Bot

Automates hitting recreation.gov at exactly 10:00 AM ET when Loop C sites (1–34) are released.

## How it works

```
T - 2 min   Browser launches → logs in → pre-loads campground page
T - 2 sec   Starts polling recreation.gov's internal availability API
T + 0:00    Sites become available → bot adds first open site to cart
            → walks checkout flow → PAUSES for you to click "Reserve"
```

Three components work together:

| File | Role |
|------|------|
| `timer.py` | NTP-synced clock — corrects system clock drift |
| `availability.py` | Queries the internal JSON API (not HTML scraping) |
| `booker.py` | Playwright browser — logs in, clicks "Add to Cart", walks checkout |
| `main.py` | Orchestrates everything |

## Setup

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install Playwright browsers (only needed once)
playwright install chromium

# 3. Edit config.yaml
#    - Set your recreation.gov email/password
#    - Set your desired checkin_date and checkout_date
#    - Adjust preferred_sites if you want to include/exclude specific sites
```

## Usage

```bash
# Check current availability right now (no booking)
python main.py --check-only

# Full run: wait for 10 AM, then book
python main.py

# Dry run: goes through timing/polling but skips actual browser booking
python main.py --dry-run

# Use a different config file
python main.py --config ~/my_trip.yaml
```

## Configuration (`config.yaml`)

```yaml
recreation_gov:
  campground_id: "233687"        # Goose Point — don't change this
  email: "you@example.com"
  password: "yourpassword"

booking:
  checkin_date: "2025-06-01"
  checkout_date: "2025-06-03"
  preferred_sites:               # Loop C sites you'll accept (tried in order)
    - "1"
    - "2"
    # ... up to 34

timing:
  timezone: "America/New_York"   # Release is always 10 AM ET
  preflight_seconds: 120         # Log in this many seconds early
  poll_start_seconds: 2          # Start API polling this many seconds early
  poll_interval_seconds: 0.3     # How fast to re-poll

browser:
  headless: false                # Keep false so you can see what's happening
```

## Tips for Loop C

- **Run `--check-only` the day before** to verify your dates and site list resolve correctly.
- **Have a stable internet connection** — Wi-Fi is fine but wired is better.
- **Don't close the browser** after the bot adds to cart; finish the checkout in that window.
- The bot stops at the final "Reserve"/"Place Order" button intentionally so you can review before committing.
- If you miss a site at 10 AM, run `--check-only` periodically — cancellations do happen.

## Disclaimer

This tool is for personal use to assist with a single booking. Do not leave it running continuously or use it to poll at high frequency — recreation.gov monitors network usage and may suspend accounts. Always review their [Terms of Service](https://www.recreation.gov/terms) before use.

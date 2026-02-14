"""
Card Show Finder — scrapes TCDB for upcoming card shows, checks driving
distance from team member addresses, and pings Discord if within 2 hours.

Local mode  : reads DISCORD_WEBHOOK.txt, TARGET_STATES.txt, TEAM_ADDRESSES.txt
GitHub Actions: reads from repository secrets via env vars
"""

import os
import re
import sys
import time
import json
import urllib.parse

# Fix emoji output on Windows terminals (cp1252 doesn't support them)
if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("cp"):
    import io
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace"
    )

import requests
from bs4 import BeautifulSoup

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

# ── constants ────────────────────────────────────────────────
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_ids.txt")
MAX_TRAVEL_SECONDS = 7200          # 2 hours
REQUEST_DELAY = 2                  # polite delay between TCDB page loads
NOMINATIM_DELAY = 1.1              # Nominatim ToS: max 1 req/s
USER_AGENT = "CardShowBot/1.0 (github-action-card-show-finder)"
BASE_URL = "https://www.tcdb.com"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ── configuration ────────────────────────────────────────────
def _read_txt(filename):
    """Return non-blank stripped lines from a text file (resolved relative to script dir)."""
    filepath = os.path.join(SCRIPT_DIR, filename)
    if not os.path.exists(filepath):
        return []
    with open(filepath, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def load_config():
    """
    Load settings from env vars first (GitHub Actions), then fall back
    to local .txt files.
    """
    # Discord webhooks — env var is comma-separated
    wh_env = os.getenv("DISCORD_WEBHOOKS", "").strip()
    webhooks = [u.strip() for u in wh_env.split(",") if u.strip()] if wh_env else []
    if not webhooks:
        webhooks = _read_txt("DISCORD_WEBHOOK.txt")

    # Target states — env var is comma-separated (e.g. "VA,MD,DC")
    st_env = os.getenv("TARGET_STATES", "").strip()
    states = [s.strip() for s in st_env.split(",") if s.strip()] if st_env else []
    if not states:
        states = _read_txt("TARGET_STATES.txt") or ["VA"]

    # Team addresses — env var is pipe-separated, txt is one per line
    ta_env = os.getenv("TEAM_ADDRESSES", "").strip()
    addresses = [a.strip() for a in ta_env.split("|") if a.strip()] if ta_env else []
    if not addresses:
        addresses = _read_txt("TEAM_ADDRESSES.txt")

    # Register secrets with GitHub Actions log masking
    for wh in webhooks:
        _gh_mask(wh)
    for addr in addresses:
        _gh_mask(addr)

    return webhooks, states, addresses


# ── logging helpers ──────────────────────────────────────────
def _mask(text, show_chars=8):
    """Mask sensitive text for safe logging. Shows last `show_chars` chars."""
    if len(text) <= show_chars:
        return "****"
    return f"****{text[-show_chars:]}"


def _mask_address(addr):
    """Show only city/state from an address for logging."""
    parts = [p.strip() for p in addr.split(",")]
    if len(parts) >= 2:
        return f"****{parts[-2]}, {parts[-1]}"
    return "****"


def _gh_mask(secret):
    """Tell GitHub Actions to mask a value in all future log output."""
    if os.getenv("GITHUB_ACTIONS"):
        print(f"::add-mask::{secret}")


# ── HTTP helper ──────────────────────────────────────────────
def make_scraper():
    """Return a cloudscraper session if available, else plain requests."""
    if cloudscraper is not None:
        s = cloudscraper.create_scraper()
        print("🌐 Using cloudscraper")
        return s
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    print("🌐 Using plain requests (install cloudscraper for Cloudflare bypass)")
    return s


# ── geocoding & routing (free APIs) ─────────────────────────
def _nominatim_lookup(query):
    """Single Nominatim search.  Returns (lon, lat) or None."""
    encoded = urllib.parse.quote(query)
    url = (
        f"https://nominatim.openstreetmap.org/search"
        f"?q={encoded}&format=json&countrycodes=us&limit=1"
    )
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        data = r.json()
        if data:
            return (float(data[0]["lon"]), float(data[0]["lat"]))
    except Exception as e:
        print(f"    ⚠️  Nominatim error: {e}")
    return None


def geocode(address, quiet=False):
    """
    Try progressively simpler address strings until Nominatim returns
    a result.  Returns (lon, lat) or None.
    Set quiet=True to suppress address/coordinate logging (for personal addresses).
    """
    clean = re.sub(r"\s+", " ", address.replace("\n", " ").replace("\r", "")).strip()
    clean = re.sub(r",?\s*United States\s*$", "", clean).strip()

    attempts = [clean]

    # Attempt: from first digit onward (drops venue name)
    m = re.search(r"\d", clean)
    if m:
        attempts.append(clean[m.start():])

    # Attempt: just City, State Zip (last two comma-parts)
    parts = [p.strip() for p in clean.split(",")]
    if len(parts) >= 2:
        attempts.append(f"{parts[-2]}, {parts[-1]}")

    for attempt in attempts:
        coords = _nominatim_lookup(attempt)
        if coords:
            if quiet:
                print(f"    📍 Geocoded successfully")
            else:
                print(f"    📍 Geocoded: {attempt}  →  ({coords[1]:.4f}, {coords[0]:.4f})")
            return coords
        time.sleep(NOMINATIM_DELAY)

    if quiet:
        print(f"    ⚠️  Could not geocode address")
    else:
        print(f"    ⚠️  Could not geocode: {clean}")
    return None


def get_driving_seconds(start, end):
    """OSRM public demo server — returns seconds or inf on failure."""
    url = (
        f"http://router.project-osrm.org/route/v1/driving/"
        f"{start[0]},{start[1]};{end[0]},{end[1]}?overview=false"
    )
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if data.get("code") == "Ok" and data.get("routes"):
            return data["routes"][0]["duration"]
    except Exception as e:
        print(f"    ⚠️  OSRM error: {e}")
    return float("inf")


# ── TCDB scraping ────────────────────────────────────────────
def parse_listing_page(html):
    """
    Parse a TCDB state-listing page and return a list of show dicts.

    HTML structure (per show):
        <p><strong>Saturday, February 14, 2026</strong></p>
        <ul>
          <li>
            <a href="…CardShows.cfm?MODE=VIEW&ID=16699">Show Name</a><br>
            Venue<br>
            City, ST<br>
            Time
          </li>
        </ul>
    """
    soup = BeautifulSoup(html, "html.parser")
    shows = []
    current_date = "Unknown"

    # Only look inside the content div to avoid nav/footer matches
    content = soup.find("div", id="content")
    if not content:
        print("  ⚠️  Could not find #content div")
        return shows

    for el in content.find_all(["p", "li"]):
        # Date headers
        if el.name == "p":
            strong = el.find("strong")
            if strong:
                current_date = strong.get_text(strip=True)
            continue

        # Show entries
        if el.name == "li":
            link = el.find(
                "a",
                href=re.compile(r"CardShows\.cfm\?MODE=VIEW&(?:amp;)?ID=\d+"),
            )
            if not link:
                continue
            id_match = re.search(r"ID=(\d+)", link["href"])
            if not id_match:
                continue

            show_id = id_match.group(1)
            show_name = link.get_text(strip=True)

            # Remaining text after the link → venue / city / time
            lines = [
                l.strip()
                for l in el.get_text(separator="\n", strip=True).split("\n")
                if l.strip()
            ]
            # lines[0] = show name, [1] = venue, [2] = city,ST, [3] = time
            venue = lines[1] if len(lines) > 1 else ""
            city_state = lines[2] if len(lines) > 2 else ""
            show_time = lines[3] if len(lines) > 3 else ""

            shows.append(
                {
                    "id": show_id,
                    "name": show_name,
                    "date": current_date,
                    "venue": venue,
                    "city_state": city_state,
                    "time": show_time,
                    "url": f"{BASE_URL}/CardShows.cfm?MODE=VIEW&ID={show_id}",
                }
            )

    return shows


def parse_detail_page(html):
    """
    Parse a TCDB show-detail page.  Returns the full street address string
    or None.

    HTML structure after the heading:
        <h3 class="site">Coastal Card Shows</h3>
        <p>Saturday, March 21, 2026 (9:00 AM - 3:00 PM)</p>
        <p>Grassfield Ruritan Club<br>920 Shillelagh Rd<br>
           Chesapeake, VA 23323<br>United States</p>
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find the show-name heading (h3.site that isn't "Card Shows")
    show_h3 = None
    for h3 in soup.find_all("h3", class_="site"):
        if h3.get_text(strip=True) != "Card Shows":
            show_h3 = h3
            break

    if not show_h3:
        return None

    # Collect the next few <p> siblings
    p_tags = []
    sib = show_h3.find_next_sibling()
    while sib and len(p_tags) < 6:
        if getattr(sib, "name", None) == "p":
            p_tags.append(sib)
        sib = sib.find_next_sibling() if sib else None

    # The address <p> is the one with a 5-digit zip code
    for p in p_tags:
        text = p.get_text(separator=", ", strip=True)
        if re.search(r"\b\d{5}\b", text):
            # Strip trailing "United States"
            text = re.sub(r",?\s*United States\s*$", "", text).strip()
            return text

    # Fallback: second <p> after heading (typical position)
    if len(p_tags) >= 2:
        text = p_tags[1].get_text(separator=", ", strip=True)
        text = re.sub(r",?\s*United States\s*$", "", text).strip()
        return text

    return None


# ── Discord ──────────────────────────────────────────────────
def send_discord_alert(webhooks, show):
    """Post a rich-embed card-show alert to each Discord webhook."""
    embed = {
        "title": f"🃏 {show['name']}",
        "url": show["url"],
        "color": 0x2ECC71,
        "fields": [
            {"name": "📅 Date", "value": show.get("date", "TBD"), "inline": True},
            {"name": "🕐 Time", "value": show.get("time", "TBD"), "inline": True},
            {"name": "📍 Address", "value": show.get("address", "See link"), "inline": False},
            {
                "name": "🚗 Closest Drive",
                "value": show.get("drive_time_str", "< 2 hrs"),
                "inline": True,
            },
        ],
    }
    payload = {
        "content": "🏟️ **New Card Show Alert!**",
        "embeds": [embed],
    }

    for wh in webhooks:
        try:
            r = requests.post(wh, json=payload, timeout=10)
            if r.status_code < 300:
                print(f"    ✅ Discord notified!")
            else:
                print(f"    ❌ Discord error ({r.status_code}) on webhook {_mask(wh)}")
        except Exception as e:
            print(f"    ❌ Discord exception on webhook {_mask(wh)}: {e}")
        time.sleep(0.5)


# ── main ─────────────────────────────────────────────────────
def main():
    webhooks, states, team_addresses = load_config()

    print("=" * 60)
    print("  Card Show Finder")
    print("=" * 60)
    print(f"  States       : {', '.join(states)}")
    print(f"  Team addrs   : {len(team_addresses)}")
    print(f"  Webhooks     : {len(webhooks)}")
    print()

    if not webhooks:
        print("⚠️  No Discord webhooks configured — results will only be logged.")
    if not team_addresses:
        print("❌ No team addresses configured. Cannot calculate distances.")
        sys.exit(1)

    # ── geocode team members ──
    print("📍 Geocoding team member addresses …")
    team_coords = []
    for addr in team_addresses:
        print(f"  → {_mask_address(addr)}")
        coords = geocode(addr, quiet=True)
        if coords:
            team_coords.append(coords)
        else:
            print(f"    ⚠️  FAILED — skipping this address")
        time.sleep(NOMINATIM_DELAY)

    if not team_coords:
        print("❌ CRITICAL: No team addresses could be geocoded. Exiting.")
        sys.exit(1)
    print(f"  ✅ {len(team_coords)}/{len(team_addresses)} addresses geocoded\n")

    # ── load seen IDs ──
    seen_ids = set(_read_txt(SEEN_FILE))
    print(f"📋 {len(seen_ids)} previously seen show IDs\n")

    scraper = make_scraper()
    new_alerts = 0

    # ── per-state loop ──
    for state in states:
        list_url = (
            f"{BASE_URL}/CardShows.cfm"
            f"?MODE=Location&State={state}&Country=United%20States"
        )
        print(f"\n{'─'*60}")
        print(f"🔍 {state} — {list_url}")

        try:
            resp = scraper.get(list_url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ❌ Could not fetch listing page: {e}")
            continue

        shows = parse_listing_page(resp.text)
        print(f"  📄 Found {len(shows)} total shows listed")

        for show in shows:
            sid = show["id"]
            if sid in seen_ids:
                continue

            print(f"\n  🆕 [{sid}] {show['name']}")
            print(f"     {show['date']}  {show['time']}")
            print(f"     {show['venue']}, {show['city_state']}")

            # Mark seen immediately so we don't re-process on failure
            seen_ids.add(sid)
            with open(SEEN_FILE, "a", encoding="utf-8") as f:
                f.write(f"{sid}\n")

            # ── fetch detail page for full address ──
            time.sleep(REQUEST_DELAY)
            try:
                detail_resp = scraper.get(show["url"], timeout=30)
                detail_resp.raise_for_status()
            except Exception as e:
                print(f"     ⚠️  Could not fetch detail page: {e}")
                continue

            address = parse_detail_page(detail_resp.text)
            if not address:
                print("     ⚠️  Could not extract address from detail page")
                continue

            show["address"] = address
            print(f"     📍 {address}")

            # ── geocode show venue ──
            time.sleep(NOMINATIM_DELAY)
            show_coords = geocode(address)
            if not show_coords:
                print("     ⚠️  Geocoding failed — skipping distance check")
                continue

            # ── check drive time from each team member ──
            min_drive = float("inf")
            for tc in team_coords:
                dt = get_driving_seconds(tc, show_coords)
                if dt < min_drive:
                    min_drive = dt
                time.sleep(0.5)          # be nice to OSRM

            if min_drive <= MAX_TRAVEL_SECONDS:
                hrs = min_drive / 3600
                show["drive_time_str"] = f"~{hrs:.1f} hrs"
                print(f"     ✅ WITHIN RANGE — {show['drive_time_str']}")
                new_alerts += 1
                if webhooks:
                    send_discord_alert(webhooks, show)
            else:
                if min_drive == float("inf"):
                    print("     ⏭️  Could not calculate drive time")
                else:
                    print(f"     ⏭️  Out of range ({min_drive/3600:.1f} hrs)")

    # ── summary ──
    print(f"\n{'='*60}")
    print(f"  Done!  {new_alerts} new show(s) within range.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

import os
import cloudscraper
from bs4 import BeautifulSoup
import requests
import time
import re

# 1. Configuration & Secrets
WEBHOOKS = [url.strip() for url in os.getenv("DISCORD_WEBHOOKS", "").split(",") if url.strip()]
STATES = [s.strip() for s in os.getenv("TARGET_STATES", "VA").split(",") if s.strip()]
TEAM_ADDRESSES = [a.strip() for a in os.getenv("TEAM_ADDRESSES", "").split(",") if a.strip()]
SEEN_FILE = "seen_ids.txt"
MAX_TRAVEL_TIME = 7200  # 2 hours in seconds

scraper = cloudscraper.create_scraper()

def get_coords(address):
    """Geocode address to Lat/Lon using OpenStreetMap"""
    try:
        # Nominatim requires a user-agent
        url = f"https://nominatim.openstreetmap.org/search?q={address}&format=json&limit=1"
        res = requests.get(url, headers={'User-Agent': 'CardShowBot/1.0'}, timeout=10).json()
        if res:
            return (res[0]['lon'], res[0]['lat'])
    except Exception as e:
        print(f"Geocoding error for {address}: {e}")
    return None

def get_travel_time(start_coords, end_coords):
    """Calculate driving duration via OSRM public API"""
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{start_coords[0]},{start_coords[1]};{end_coords[0]},{end_coords[1]}?overview=false"
        res = requests.get(url, timeout=10).json()
        return res['routes'][0]['duration'] if 'routes' in res else float('inf')
    except Exception as e:
        print(f"OSRM error: {e}")
        return float('inf')

# Pre-geocode team addresses to save API calls
print("Geocoding team addresses...")
team_coords = []
for addr in TEAM_ADDRESSES:
    coords = get_coords(addr)
    if coords:
        team_coords.append(coords)
    time.sleep(1) # Respect Nominatim rate limits

# 2. Load History
if os.path.exists(SEEN_FILE):
    with open(SEEN_FILE, "r") as f:
        seen_ids = set(f.read().splitlines())
else:
    seen_ids = set()

# 3. Scrape Loop
new_ids = []
for state in STATES:
    print(f"Checking state: {state}...")
    target_url = f"https://www.tcdb.com/CardShows.cfm?MODE=Location&State={state}&Country=United%20States"
    
    try:
        response = scraper.get(target_url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        for link in soup.find_all('a', href=True):
            if "ID=" in link['href']:
                show_id = link['href'].split("ID=")[-1]
                
                if show_id not in seen_ids:
                    show_url = f"https://www.tcdb.com/{link['href']}"
                    print(f"Found new show ID {show_id}, checking details...")
                    
                    detail_res = scraper.get(show_url)
                    detail_soup = BeautifulSoup(detail_res.text, 'html.parser')
                    
                    # --- Robust Address Parsing ---
                    show_address = ""
                    # Look for strings in the main content area
                    content_strings = list(detail_soup.stripped_strings)
                    
                    for i, text in enumerate(content_strings):
                        # Pattern: Find a line with a Zip Code (5 digits, often end of address)
                        if re.search(r'\b\d{5}\b', text):
                            # The address is usually the Zip line + the 1-2 lines before it (Venue/Street)
                            parts = content_strings[max(0, i-2) : i+1]
                            show_address = ", ".join(parts)
                            break
                    
                    if show_address:
                        print(f"Extracted Address: {show_address}")
                        show_coords = get_coords(show_address)
                        
                        if show_coords:
                            # 4. Distance Calculation
                            within_range = False
                            for t_coord in team_coords:
                                if get_travel_time(t_coord, show_coords) <= MAX_TRAVEL_TIME:
                                    within_range = True
                                    break
                            
                            if within_range:
                                msg = f"🏎️ **Show Found within 2hrs!**\n**{link.text}**\n📍 {show_address}\n🔗 {show_url}"
                                for wh in WEBHOOKS:
                                    requests.post(wh, json={"content": msg})
                                    print("Notification sent to Discord!")
                        else:
                            print(f"Could not geocode address: {show_address}")
                    
                    seen_ids.add(show_id)
                    new_ids.append(show_id)
                    time.sleep(2) # Be kind to TCDB
                    
    except Exception as e:
        print(f"Error scraping {state}: {e}")

# 5. Save History
if new_ids:
    with open(SEEN_FILE, "w") as f:
        f.write("\n".join(seen_ids))
    print(f"Saved {len(new_ids)} new IDs to history.")
else:
    print("No new shows found.")

import os
import cloudscraper
from bs4 import BeautifulSoup
import requests
import time
import re
import sys

# 1. Configuration & Secrets
WEBHOOKS = [url.strip() for url in os.getenv("DISCORD_WEBHOOKS", "").split(",") if url.strip()]
STATES = [s.strip() for s in os.getenv("TARGET_STATES", "VA").split(",") if s.strip()]
# CHANGED: Now splitting by Pipe (|) to allow commas inside addresses
TEAM_ADDRESSES = [a.strip() for a in os.getenv("TEAM_ADDRESSES", "").split("|") if a.strip()]
SEEN_FILE = "seen_ids.txt"
MAX_TRAVEL_TIME = 7200 # 2 hours in seconds

scraper = cloudscraper.create_scraper()

import urllib.parse

def get_coords(address):
    """Converts text address to Lat/Lon. Slims down the address if the first attempt fails."""
    # Clean standard junk
    clean_addr = address.replace("United States", "").strip().replace("\n", " ").replace("\r", "")
    
    # Try 1: The full address (Building + Street + City)
    coords = call_nominatim(clean_addr)
    if coords:
        return coords

    # Try 2: If Try 1 fails, strip out common building names and try just the Street/City
    # We look for the first number (the street number) and start from there
    match = re.search(r'\d+', clean_addr)
    if match:
        slim_addr = clean_addr[match.start():]
        print(f"🔄 Retrying with slim address: {slim_addr}")
        return call_nominatim(slim_addr)
        
    return None

def call_nominatim(query):
    """Internal helper to hit the Map API"""
    import urllib.parse
    encoded_addr = urllib.parse.quote(query)
    url = f"https://nominatim.openstreetmap.org/search?q={encoded_addr}&format=json&limit=1"
    headers = {'User-Agent': 'CardShowBot/1.0'}
    try:
        res = requests.get(url, headers=headers, timeout=10).json()
        if res:
            return (res[0]['lon'], res[0]['lat'])
    except:
        pass
    return None

def get_travel_time(start_coords, end_coords):
    """Calculates driving duration via OSRM public API"""
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{start_coords[0]},{start_coords[1]};{end_coords[0]},{end_coords[1]}?overview=false"
        res = requests.get(url, timeout=10).json()
        return res['routes'][0]['duration'] if 'routes' in res else float('inf')
    except Exception as e:
        print(f"OSRM error: {e}")
        return float('inf')

# --- INITIALIZATION ---

print("Geocoding team addresses...")
team_coords = []
for addr in TEAM_ADDRESSES:
    coords = get_coords(addr)
    if not coords:
        # If this fails, it prints exactly what it tried to find
        print(f"❌ CRITICAL: Could not find your address: {addr}")
        sys.exit(1)
    team_coords.append(coords)
    time.sleep(1)

if os.path.exists(SEEN_FILE):
    with open(SEEN_FILE, "r") as f:
        seen_ids = set(f.read().splitlines())
else:
    seen_ids = set()

# --- MAIN LOOP ---

for state in STATES:
    print(f"\n--- Checking {state} ---")
    list_url = f"https://www.tcdb.com/CardShows.cfm?MODE=Location&State={state}&Country=United%20States"
    
    try:
        response = scraper.get(list_url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        for link in soup.find_all('a', href=True):
            if "ID=" in link['href']:
                show_id = link['href'].split("ID=")[-1]
                
                if show_id in seen_ids:
                    continue

                show_url = f"https://www.tcdb.com/{link['href']}"
                print(f"Processing Show {show_id}...")
                
                detail_res = scraper.get(show_url)
                detail_soup = BeautifulSoup(detail_res.text, 'html.parser')
                p_tags = detail_soup.find_all('p')

                # Using Position Index: Date is p[0], Address is p[1]
                if len(p_tags) > 1:
                    show_date = p_tags[0].get_text(strip=True)
                    show_address = p_tags[1].get_text(separator=" ", strip=True)

                    if re.search(r'\b\d{5}\b', show_address):
                        show_coords = get_coords(show_address)
                        
                        if show_coords:
                            in_range = any(get_travel_time(tc, show_coords) <= MAX_TRAVEL_TIME for tc in team_coords)
                            
                            if in_range:
                                msg = f"🏎️ **New Show Alert!**\n**{link.text}**\n📅 {show_date}\n📍 {show_address}\n🔗 {show_url}"
                                for wh in WEBHOOKS:
                                    requests.post(wh, json={"content": msg})
                                print(f"✅ Notified for {show_id}")
                            else:
                                print(f"⏭️ {show_id} is outside range.")
                        else:
                            print(f"🛑 Geocoder failed on show {show_id}")
                            sys.exit(1)
                    
                    with open(SEEN_FILE, "a") as f:
                        f.write(f"{show_id}\n")
                    seen_ids.add(show_id)
                    time.sleep(2)

    except Exception as e:
        print(f"Error on {state}: {e}")

print("\nAll done!")

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
    """Converts text address to Lat/Lon with strict encoding."""
    # Clean the address of any hidden newlines or tabs
    clean_addr = address.replace("United States", "").strip().replace("\n", " ").replace("\r", "")
    
    # URL encode the address (turns spaces into %20, etc.)
    encoded_addr = urllib.parse.quote(clean_addr)
    
    try:
        url = f"https://nominatim.openstreetmap.org/search?q={encoded_addr}&format=json&limit=1"
        
        # Use a very specific User-Agent as required by Nominatim's Policy
        headers = {
            'User-Agent': 'CardShowBot/1.0 (contact: your-github-username)' 
        }
        
        res = requests.get(url, headers=headers, timeout=10).json()
        
        if res:
            print(f"✅ Found Coords for: {clean_addr}")
            return (res[0]['lon'], res[0]['lat'])
        else:
            print(f"⚠️ Map API returned no results for: [{clean_addr}]")
            return None
    except Exception as e:
        print(f"❌ Geocoding error: {e}")
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

import os
import cloudscraper
from bs4 import BeautifulSoup
import requests
import time
import re
import sys
import urllib.parse

# 1. Configuration & Secrets
WEBHOOKS = [url.strip() for url in os.getenv("DISCORD_WEBHOOKS", "").split(",") if url.strip()]
STATES = [s.strip() for s in os.getenv("TARGET_STATES", "VA").split(",") if s.strip()]
TEAM_ADDRESSES = [a.strip() for a in os.getenv("TEAM_ADDRESSES", "").split("|") if a.strip()]
SEEN_FILE = "seen_ids.txt"
MAX_TRAVEL_TIME = 7200 # 2 hours in seconds

scraper = cloudscraper.create_scraper()

def call_nominatim(query):
    """Helper to hit the Map API with proper encoding and headers"""
    encoded_addr = urllib.parse.quote(query)
    url = f"https://nominatim.openstreetmap.org/search?q={encoded_addr}&format=json&limit=1"
    headers = {'User-Agent': 'CardShowBot/1.0 (Contact: YourGitHubUser)'}
    try:
        res = requests.get(url, headers=headers, timeout=10).json()
        return (res[0]['lon'], res[0]['lat']) if res else None
    except:
        return None

def get_coords(address):
    """Aggressively tries to find coordinates by slimming down the address."""
    clean_addr = address.replace("United States", "").strip().replace("\n", " ").replace("\r", "")
    
    # Try 1: Full Address
    coords = call_nominatim(clean_addr)
    if coords: return coords

    # Try 2: Start from the first number (Street Number)
    match = re.search(r'\d+', clean_addr)
    if match:
        slim_addr = clean_addr[match.start():]
        print(f"🔄 Retrying with street: {slim_addr}")
        coords = call_nominatim(slim_addr)
        if coords: return coords

    # Try 3: Last Resort - Just City, State, Zip
    # We look for the last comma and take everything after it
    parts = clean_addr.split(',')
    if len(parts) >= 2:
        city_zip = f"{parts[-2].strip()}, {parts[-1].strip()}"
        print(f"📍 Last resort (City/Zip): {city_zip}")
        return call_nominatim(city_zip)
        
    return None

def get_travel_time(start_coords, end_coords):
    """Calculates driving duration via OSRM public API"""
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{start_coords[0]},{start_coords[1]};{end_coords[0]},{end_coords[1]}?overview=false"
        res = requests.get(url, timeout=10).json()
        return res['routes'][0]['duration'] if 'routes' in res else float('inf')
    except:
        return float('inf')

# --- INITIALIZATION ---
print("Geocoding team addresses...")
team_coords = [get_coords(a) for a in TEAM_ADDRESSES if get_coords(a)]
if not team_coords:
    print(f"❌ CRITICAL: Could not find any team addresses. Check your secret.")
    sys.exit(1)

seen_ids = set(open(SEEN_FILE).read().splitlines()) if os.path.exists(SEEN_FILE) else set()

# --- MAIN LOOP ---
for state in STATES:
    print(f"\n--- Checking {state} (Team: {len(team_coords)} locations) ---")
    list_url = f"https://www.tcdb.com/CardShows.cfm?MODE=Location&State={state}&Country=United%20States"
    
    try:
        response = scraper.get(list_url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        for link in soup.find_all('a', href=True):
            if "ID=" in link['href']:
                show_id = link['href'].split("ID=")[-1]
                if show_id in seen_ids: continue

                show_url = f"https://www.tcdb.com/{link['href']}"
                print(f"Processing Show {show_id}...")
                
                detail_res = scraper.get(show_url)
                p_tags = BeautifulSoup(detail_res.text, 'html.parser').find_all('p')

                if len(p_tags) > 1:
                    show_date = p_tags[0].get_text(strip=True)
                    show_address = p_tags[1].get_text(separator=" ", strip=True)

                    if re.search(r'\b\d{5}\b', show_address):
                        show_coords = get_coords(show_address)
                        
                        if show_coords:
                            in_range = any(get_travel_time(tc, show_coords) <= MAX_TRAVEL_TIME for tc in team_coords)
                            
                            if in_range:
                                payload = {"content": f"🏎️ **New Show Alert!**\n**{link.text}**\n📅 {show_date}\n📍 {show_address}\n🔗 {show_url}"}
                                for wh in WEBHOOKS:
                                    r = requests.post(wh, json=payload)
                                    if r.status_code >= 300:
                                        print(f"❌ Discord Error ({r.status_code}): {r.text}")
                                    else:
                                        print(f"✅ Notified for {show_id}")
                            else:
                                print(f"⏭️ {show_id} is outside range.")
                        else:
                            print(f"⚠️ Skipping {show_id}: Address could not be geocoded.")
                    
                    with open(SEEN_FILE, "a") as f: f.write(f"{show_id}\n")
                    seen_ids.add(show_id)
                    time.sleep(2)
    except Exception as e:
        print(f"Error on {state}: {e}")

print("\nAll done!")

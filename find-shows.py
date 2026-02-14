import os
import cloudscraper
from bs4 import BeautifulSoup
import requests
import time

# 1. Configuration from Secrets
WEBHOOKS = os.getenv("DISCORD_WEBHOOKS", "").split(",")
STATES = os.getenv("TARGET_STATES", "VA").split(",")
TEAM_ADDRESSES = os.getenv("TEAM_ADDRESSES", "").split(",")
SEEN_FILE = "seen_ids.txt"
MAX_TRAVEL_TIME = 7200  # 2 hours in seconds

scraper = cloudscraper.create_scraper()

def get_coords(address):
    """Geocode address to Latitude/Longitude using OpenStreetMap"""
    try:
        url = f"https://nominatim.openstreetmap.org/search?q={address}&format=json&limit=1"
        # User-Agent is required by Nominatim policy
        res = requests.get(url, headers={'User-Agent': 'CardShowBot/1.0'}).json()
        return (res[0]['lon'], res[0]['lat']) if res else None
    except: return None

def get_travel_time(start_coords, end_coords):
    """Calculate driving duration via OSRM public API"""
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{start_coords[0]},{start_coords[1]};{end_coords[0]},{end_coords[1]}?overview=false"
        res = requests.get(url).json()
        return res['routes'][0]['duration'] if 'routes' in res else float('inf')
    except: return float('inf')

# Pre-geocode team addresses once to save API calls
team_coords = [get_coords(a) for a in TEAM_ADDRESSES if get_coords(a)]

# 2. Load History
if os.path.exists(SEEN_FILE):
    with open(SEEN_FILE, "r") as f:
        seen_ids = set(f.read().splitlines())
else:
    seen_ids = set()

# 3. Main Loop: Iterate through each State URL
new_ids = []
for state in STATES:
    state = state.strip()
    # TCDB URL structure: State=XX
    target_url = f"https://www.tcdb.com/CardShows.cfm?MODE=Location&State={state}&Country=United%20States"
    
    response = scraper.get(target_url)
    soup = BeautifulSoup(response.text, 'html.parser')
    
    for link in soup.find_all('a', href=True):
        if "ID=" in link['href']:
            show_id = link['href'].split("ID=")[-1]
            if show_id not in seen_ids:
                # 4. Scrape Detail Page for Address
                show_url = f"https://www.tcdb.com/{link['href']}"
                detail_res = scraper.get(show_url)
                detail_soup = BeautifulSoup(detail_res.text, 'html.parser')
                
                # TCDB usually puts the venue address in a specific table/div
                # We search for the text that looks like an address
                address_tag = detail_soup.find("h5", text="Location")
                show_address = ""
                if address_tag:
                    # Finds the text immediately following the "Location" header
                    show_address = address_tag.find_next_sibling(text=True).strip()

                if show_address:
                    show_coords = get_coords(show_address)
                    if show_coords:
                        # 5. Check travel time against every team member
                        is_valid = False
                        for t_coord in team_coords:
                            if get_travel_time(t_coord, show_coords) <= MAX_TRAVEL_TIME:
                                is_valid = True
                                break
                        
                        if is_valid:
                            msg = f"🏎️ **Show Found within 2hrs!**\n**{link.text}**\n📍 {show_address}\n🔗 {show_url}"
                            for wh in WEBHOOKS:
                                requests.post(wh, json={"content": msg})
                
                seen_ids.add(show_id)
                new_ids.append(show_id)
                time.sleep(1.5) # Sleep to avoid Cloudflare triggers

# 6. Update History File
if new_ids:
    with open(SEEN_FILE, "w") as f:
        f.write("\n".join(seen_ids))

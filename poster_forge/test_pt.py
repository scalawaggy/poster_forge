import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from config import load_settings

settings = load_settings()
radarr_url = settings.get("RADARR_URL")
radarr_key = settings.get("RADARR_API_KEY")
add_one_day = settings.get("ADD_ONE_DAY_MOVIES", False)
print(f"ADD ONE DAY is currently: {add_one_day}")

now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
now_pt = now_utc.astimezone(ZoneInfo("America/Los_Angeles"))

res = requests.get(f"{radarr_url}/api/v3/movie", headers={"X-Api-Key": radarr_key}, timeout=10)
for m in res.json():
    title = m.get("title")
    if "Hold the Fort" in title or "The Get Out" in title:
        print(f"--- {title} ---")
        digital = m.get("digitalRelease")
        cinema = m.get("inCinemas")
        print(f"Original Digital UTC: {digital}")
        
        target_str = digital or cinema
        if target_str:
            target_utc = datetime.strptime(target_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
            target_pt = target_utc.astimezone(ZoneInfo("America/Los_Angeles"))
            print(f"Target PT before offset: {target_pt}")
            
            if add_one_day:
                target_pt += timedelta(days=1)
                
            days_left = (target_pt.date() - now_pt.date()).days
            print(f"Days Left (from local PT {now_pt}): {days_left}")

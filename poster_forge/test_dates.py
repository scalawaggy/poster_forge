import sys
from datetime import datetime, timedelta
import requests
from config import load_settings

settings = load_settings()
radarr_url = settings.get("RADARR_URL")
radarr_key = settings.get("RADARR_API_KEY")
add_one_day = settings.get("ADD_ONE_DAY_MOVIES", False)
now = datetime.utcnow()

res = requests.get(f"{radarr_url}/api/v3/movie", headers={"X-Api-Key": radarr_key}, timeout=10)
for m in res.json():
    title = m.get("title")
    if "Hold the Fort" in title or "The Get Out" in title:
        print(f"--- {title} ---")
        digital = m.get("digitalRelease")
        cinema = m.get("inCinemas")
        print(f"Original Digital: {digital}")
        print(f"Original Cinema: {cinema}")
        
        target_str = digital or cinema
        if target_str:
            target_date = datetime.strptime(target_str[:10], "%Y-%m-%d")
            print(f"Parsed Target Date: {target_date}")
            if add_one_day:
                target_date += timedelta(days=1)
                target_str = target_date.strftime("%Y-%m-%d")
                print(f"After +1 Day: {target_date} / {target_str}")
                
            days_left = (target_date - now).days
            print(f"Days Left (from now {now}): {days_left}")
            
            if digital and now <= datetime.strptime(digital[:10], "%Y-%m-%d") <= now + timedelta(days=90):
                scraper_days = max(0, (datetime.strptime(digital[:10], "%Y-%m-%d") + (timedelta(days=1) if add_one_day else timedelta(days=0)) - now).days)
                print(f"Scraper Days Left: {scraper_days}")

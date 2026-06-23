from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import os
import time
from config import load_settings
from logger import sys_logger
from plexapi.server import PlexServer
from core_v3_overlay import create_and_upload_overlay
from core_v3_scraper import clean_url, get_upcoming_movies, get_upcoming_shows
from core_v3_trailer import download_and_inject_trailer
from core_v3_tv import process_returning_show
from core_plex import translate_path, restore_clean_poster
import shutil

# THE NEW DEDICATED AUDIT WRITER
def audit_log(message):
    sys_logger.info(message)
    try:
        os.makedirs("logs", exist_ok=True)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open("logs/janitor.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass

def update_v3_banners():
    audit_log("=====================================================")
    audit_log("🧹 STARTING DAILY V3 MAINTENANCE & COLLECTION AUDIT")
    audit_log("=====================================================")
    
    settings = load_settings()
    try:
        plex = PlexServer(settings.get("PLEX_URL"), settings.get("PLEX_TOKEN"))
    except Exception as e:
        audit_log(f"Maintenance failed: Could not connect to Plex: {e}")
        return
        
    radarr_url = clean_url(settings.get("RADARR_URL", "").rstrip('/'))
    radarr_key = settings.get("RADARR_API_KEY", "")
    sonarr_url = clean_url(settings.get("SONARR_URL", "").rstrip('/'))
    sonarr_key = settings.get("SONARR_API_KEY", "")

    add_one_day = settings.get("ADD_ONE_DAY_MOVIES", False)
    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
    now_pt = now_utc.astimezone(ZoneInfo("America/Los_Angeles"))
    
    # --- 1. FETCH LIVE MOVIE DATES ---
    movie_updates = {}
    if radarr_url and radarr_key:
        try:
            res = requests.get(f"{radarr_url}/api/v3/movie", headers={"X-Api-Key": radarr_key}, timeout=10)
            for m in res.json():
                title = m.get("title", "").replace(" (Trailer)", "")
                digital_date = m.get("digitalRelease") or m.get("physicalRelease")
                cinema_date = m.get("inCinemas")
                
                target_date_str = digital_date or cinema_date
                
                if target_date_str:
                    target_date_utc = datetime.strptime(target_date_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                    target_date_pt = target_date_utc.astimezone(ZoneInfo("America/Los_Angeles"))
                    
                    if add_one_day:
                        target_date_pt += timedelta(days=1)
                        
                    target_date_str = target_date_pt.strftime("%Y-%m-%d")
                    days_left = (target_date_pt.date() - now_pt.date()).days
                    is_unannounced = not digital_date
                    
                    poster_url = None
                    for img in m.get("images", []):
                        if img.get("coverType") == "poster":
                            poster_url = img.get("remoteUrl") or f"{radarr_url}{img.get('url')}&apikey={radarr_key}"
                    if poster_url:
                        movie_updates[title] = {
                            "days_left": days_left, 
                            "poster": poster_url, 
                            "date_str": target_date_str[:10],
                            "is_unannounced": is_unannounced
                        }
        except Exception: pass

    # --- 2. FETCH LIVE TV DATES ---
    show_updates = {}
    if sonarr_url and sonarr_key:
        try:
            cutoff_pt = now_pt + timedelta(days=90)
            api_url = f"{sonarr_url}/api/v3/calendar?start={now_pt.strftime('%Y-%m-%d')}&end={cutoff_pt.strftime('%Y-%m-%d')}&unmonitored=true&includeSeries=true"
            res = requests.get(api_url, headers={"X-Api-Key": sonarr_key}, timeout=10)
            for ep in res.json():
                series = ep.get("series", {})
                title = series.get("title", "")
                air_date_str = ep.get("airDateUtc")
                if air_date_str:
                    air_date_utc = datetime.strptime(air_date_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                    air_date_pt = air_date_utc.astimezone(ZoneInfo("America/Los_Angeles"))
                    days_left = (air_date_pt.date() - now_pt.date()).days
                    poster_url = None
                    for img in series.get("images", []):
                        if img.get("coverType") == "poster":
                            poster_url = img.get("remoteUrl") or f"{sonarr_url}{img.get('url')}&apikey={sonarr_key}"
                    if title not in show_updates or days_left < show_updates[title]["days_left"]:
                        if poster_url:
                            show_updates[title] = {"days_left": days_left, "poster": poster_url, "season": ep.get("seasonNumber"), "ep": ep.get("episodeNumber")}
        except Exception: pass

    def real_media_exists(trailer_item):
        clean_title = trailer_item.title.replace(" (Trailer)", "")
        tmdb_id = None
        for guid in getattr(trailer_item, 'guids', []):
            if 'tmdb://' in guid.id:
                tmdb_id = guid.id.replace('tmdb://', '')
                break
        for sec in plex.library.sections():
            if sec.type == 'movie' and "coming soon" not in sec.title.lower():
                for i in sec.search(title=clean_title):
                    if tmdb_id:
                        if any(f"tmdb://{tmdb_id}" in g.id for g in getattr(i, 'guids', [])): return True
                    else:
                        if i.title.lower() == clean_title.lower(): return True
        return False

    # --- 3. PATH A: MOVIE LIBRARY UPDATER & JANITOR ---
    audit_log("--- Evaluating Movie Libraries ---")
    for section in plex.library.sections():
        if "coming soon" in section.title.lower():
            try:
                # Get the Standard Collection inside the Coming Soon library
                collection = section.collection(title="Coming Soon")
            except Exception:
                collection = None
            
            sort_candidates = []

            for item in section.all():
                clean_title = item.title.replace(" (Trailer)", "")
                
                if real_media_exists(item):
                    audit_log(f"ASSASSIN: Real media found for '{clean_title}'. Purging trailer from NAS.")
                    try:
                        for media in item.media:
                            for part in media.parts:
                                try:
                                    real_path = translate_path(part.file)
                                    dir_to_remove = os.path.dirname(real_path)
                                    if os.path.exists(dir_to_remove):
                                        shutil.rmtree(dir_to_remove)
                                except: pass
                        item.delete()
                    except Exception: pass
                    continue 

                sort_days = 999999  # Default to very back for unannounced
                overlay_applied = False

                # 1. Check Movie Updates
                if clean_title in movie_updates:
                    data = movie_updates[clean_title]
                    days_left = data["days_left"]
                    is_unannounced = data.get("is_unannounced", False)
                    
                    try: item.edit(**{"originallyAvailableAt.value": data["date_str"], "originallyAvailableAt.locked": 1})
                    except: pass

                    if days_left <= 0 or is_unannounced: 
                        tagline = "COMING SOON" 
                        if is_unannounced:
                            sort_days = 10000 + days_left
                        else:
                            sort_days = days_left
                    else:
                        d_str = "Day" if days_left == 1 else "Days"
                        tagline = f"Releases in {days_left} {d_str}"
                        sort_days = days_left
                        
                    create_and_upload_overlay(item, data["poster"], tagline)
                    audit_log(f"🎨 Updated Banner (Movie): {clean_title} ({tagline})")
                    overlay_applied = True

                # 2. Check TV Show Updates (Fake Trailers)
                elif clean_title in show_updates:
                    data = show_updates[clean_title]
                    days_left = data["days_left"]
                    sort_days = days_left
                    
                    if days_left <= 0: tagline = "COMING SOON"
                    else:
                        d_str = "Day" if days_left == 1 else "Days"
                        tagline = f"Series Premieres in {days_left} {d_str}"
                        
                    create_and_upload_overlay(item, data["poster"], tagline)
                    audit_log(f"🎨 Updated Banner (TV Trailer): {clean_title} ({tagline})")
                    overlay_applied = True

                # Ensure item is in the standard collection so we can sort it
                try: item.addCollection(["Coming Soon"])
                except: pass
                
                sort_candidates.append((item, sort_days))

            # Apply Chronological Sort
            if sort_candidates:
                audit_log(f"📏 Enforcing chronological order for {len(sort_candidates)} Coming Soon items...")
                # Fetch collection again just in case addCollection created it
                try: collection = section.collection(title="Coming Soon")
                except: collection = None
                
                if collection:
                    sort_candidates.sort(key=lambda x: (x[1], x[0].title))
                    previous_item = None
                    position = 1
                    for item, _ in sort_candidates:
                        try:
                            if previous_item is None:
                                collection.moveItem(item)
                            else:
                                collection.moveItem(item, after=previous_item)
                            audit_log(f"   └─ Position #{position}: {item.title}")
                            previous_item = item
                            position += 1
                        except Exception as e:
                            audit_log(f"   Failed to sort {item.title}: {e}")

    # --- 4. PATH B: TV SHOW COLLECTION CHRONOLOGICAL SORTER ---
    audit_log("--- Evaluating TV Collections ---")
    for section in plex.library.sections():
        if section.type == 'show':
            try:
                collection = section.collection(title="Returning Soon")
                current_items = collection.items()
                sort_candidates = []
                
                for item in current_items:
                    if item.title in show_updates:
                        data = show_updates[item.title]
                        days_left = data["days_left"]
                        
                        if days_left <= 0: tagline = "RETURNING SOON"
                        else:
                            d_str = "Day" if days_left == 1 else "Days"
                            s_num, e_num = data["season"], data["ep"]
                            if s_num == 1 and e_num == 1: tagline = f"SERIES PREMIERE in {days_left} {d_str}"
                            elif e_num == 1 and s_num > 1: tagline = f"SEASON {s_num} Returns in {days_left} {d_str}"
                            else: tagline = f"RETURNS in {days_left} {d_str}"
                                
                        create_and_upload_overlay(item, data["poster"], tagline)
                        audit_log(f"🎨 Updated Banner: {item.title} ({tagline})")
                        sort_candidates.append((item, days_left))
                    else:
                        collection.removeItems([item])
                        audit_log(f"🧹 Removed Collection Tag: '{item.title}' (No longer upcoming in Sonarr)")
                        restore_clean_poster(item)
                        audit_log(f"Restored clean poster for '{item.title}'")
                
                # Math Sort
                sort_candidates.sort(key=lambda x: x[1])
                
                # Explicit Ordering
                if sort_candidates:
                    audit_log(f"📏 Enforcing chronological order for {len(sort_candidates)} Returning Shows...")
                    previous_item = None
                    position = 1
                    for item, _ in sort_candidates:
                        try:
                            if previous_item is None:
                                collection.moveItem(item) 
                            else:
                                collection.moveItem(item, after=previous_item) 
                            audit_log(f"   └─ Position #{position}: {item.title}")
                            previous_item = item
                            position += 1
                        except Exception as move_err:
                            audit_log(f"   Failed to sort {item.title}: {move_err}")
                            
            except Exception:
                pass

    # --- 5. AUTO-ADD PHASE ---
    if settings.get("V3_AUTO_ADD", False):
        audit_log("🤖 Auto-Add ENABLED. Hunting for new unqueued media...")
        movies = get_upcoming_movies()
        shows = get_upcoming_shows()
        for m in movies: download_and_inject_trailer(m['title'], m['tmdb_id'], m['tagline'], m['poster_url'], m.get('year'))
        for s in shows:
            if "Returns in" in s['tagline'] or "RETURNS in" in s['tagline']: process_returning_show(s['title'], s['tvdb_id'], s['tagline'], s['poster_url'])
            else: download_and_inject_trailer(s['title'], s['tvdb_id'], s['tagline'], s['poster_url'], s.get('year'))

    audit_log("Giving Plex 5 seconds to commit DB changes...")
    time.sleep(5) 
    
    audit_log("V3 MAINTENANCE COMPLETE")
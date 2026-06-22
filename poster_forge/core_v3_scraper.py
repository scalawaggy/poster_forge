import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from config import load_settings
from logger import sys_logger
from plexapi.server import PlexServer

def clean_url(url):
    if url and not url.startswith('http'):
        return 'http://' + url
    return url

def get_existing_plex_titles():
    """Securely connects to Plex and returns a list of titles already in Coming Soon OR Returning Soon."""
    settings = load_settings()
    titles = set()
    try:
        plex = PlexServer(settings.get("PLEX_URL"), settings.get("PLEX_TOKEN"))
        
        for section in plex.library.sections():
            # 1. Path A Check: Movie libraries for "Coming Soon" trailers
            if "coming soon" in section.title.lower():
                for item in section.all():
                    titles.add(item.title.replace(" (Trailer)", ""))
                    
            # 2. Path B Check: TV libraries for "Returning Soon" tags
            if section.type == 'show':
                # PlexAPI allows us to search directly by Collection tag!
                for item in section.search(collection="Returning Soon"):
                    titles.add(item.title)
                    
        return titles
    except Exception as e:
        sys_logger.error(f"Could not connect to Plex for deduplication: {e}")
        return set()

def get_upcoming_movies():
    settings = load_settings()
    radarr_url = clean_url(settings.get("RADARR_URL", "").rstrip('/'))
    radarr_key = settings.get("RADARR_API_KEY", "")
    days_out = int(settings.get("V3_DAYS_OUT", 30))
    max_items = int(settings.get("V3_MAX_MOVIES", 15))

    if not radarr_url or not radarr_key:
        sys_logger.warning("Radarr credentials missing. Skipping V3 Movie Scraper.")
        return []

    # FIX: Grab the list of titles already in Plex
    existing_titles = get_existing_plex_titles()
    upcoming = []

    try:
        headers = {"X-Api-Key": radarr_key}
        res = requests.get(f"{radarr_url}/api/v3/movie", headers=headers, timeout=10)
        res.raise_for_status()
        movies = res.json()
        add_one_day = settings.get("ADD_ONE_DAY_MOVIES", False)
        
        now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        now_pt = now_utc.astimezone(ZoneInfo("America/Los_Angeles"))
        cutoff_pt = now_pt + timedelta(days=days_out)

        for m in movies:
            title = m.get("title")
            
            # FIX: If the movie is already in Plex, skip it immediately!
            if not title or not m.get("monitored", True) or m.get("hasFile") or title in existing_titles:
                continue
                
            movie_year = m.get("year", now_pt.year)
            if movie_year < (now_pt.year - 1):
                continue

            digital_date_str = m.get("digitalRelease")
            cinema_date_str = m.get("inCinemas")
            
            digital_date_pt = None
            if digital_date_str:
                d_utc = datetime.strptime(digital_date_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                digital_date_pt = d_utc.astimezone(ZoneInfo("America/Los_Angeles"))
            
            cinema_date_pt = None
            if cinema_date_str:
                c_utc = datetime.strptime(cinema_date_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                cinema_date_pt = c_utc.astimezone(ZoneInfo("America/Los_Angeles"))

            if add_one_day:
                if digital_date_pt: digital_date_pt += timedelta(days=1)
                if cinema_date_pt: cinema_date_pt += timedelta(days=1)

            if digital_date_pt and now_pt.date() <= digital_date_pt.date() <= cutoff_pt.date():
                days_left = max(0, (digital_date_pt.date() - now_pt.date()).days)
                d_str = "Day" if days_left == 1 else "Days"
                tagline = f"Releases in {days_left} {d_str}"
                sort_weight = days_left
            elif cinema_date_pt and cinema_date_pt.date() <= now_pt.date() and not digital_date_pt:
                tagline = "COMING SOON"
                sort_weight = 999 
            else:
                continue

            poster_url = None
            for img in m.get("images", []):
                if img.get("coverType") == "poster":
                    poster_url = img.get("remoteUrl")
                    if not poster_url:
                        local_url = img.get("url")
                        if local_url:
                            sep = "&" if "?" in local_url else "?"
                            poster_url = f"{radarr_url}{local_url}{sep}apikey={radarr_key}"

            if not poster_url:
                continue

            upcoming.append({
                "title": title,
                "tmdb_id": m.get("tmdbId"),
                "year": m.get("year"),
                "type": "movie",
                "days_left": sort_weight,
                "tagline": tagline,
                "poster_url": poster_url
            })

        upcoming.sort(key=lambda x: x["days_left"])
        return upcoming[:max_items]

    except Exception as e:
        sys_logger.error(f"Radarr Scraper Crash: {e}")
        return []

def get_upcoming_shows():
    settings = load_settings()
    sonarr_url = clean_url(settings.get("SONARR_URL", "").rstrip('/'))
    sonarr_key = settings.get("SONARR_API_KEY", "")
    days_out = int(settings.get("V3_DAYS_OUT", 30))
    max_items = int(settings.get("V3_MAX_SHOWS", 15))
    min_gap = int(settings.get("V3_MIN_GAP_DAYS", 29))

    if not sonarr_url or not sonarr_key:
        sys_logger.warning("Sonarr credentials missing. Skipping V3 Show Scraper.")
        return []

    # FIX: Grab the list of titles already in Plex
    existing_titles = get_existing_plex_titles()
    upcoming = []
    added_series_ids = set()
    
    try:
        headers = {"X-Api-Key": sonarr_key}
        now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        now_pt = now_utc.astimezone(ZoneInfo("America/Los_Angeles"))
        cutoff_pt = now_pt + timedelta(days=days_out)

        start_date = now_pt.strftime("%Y-%m-%d")
        end_date = cutoff_pt.strftime("%Y-%m-%d")

        api_url = f"{sonarr_url}/api/v3/calendar?start={start_date}&end={end_date}&unmonitored=true&includeSeries=true"
        res = requests.get(api_url, headers=headers, timeout=10)
        res.raise_for_status()
        episodes = res.json()

        for ep in episodes:
            series = ep.get("series", {})
            title = series.get("title")
            series_id = series.get("id")
            
            # FIX: If the show is already in Plex, skip it immediately!
            if not title or ep.get("hasFile") or not series.get("monitored", True) or title in existing_titles:
                continue

            if series_id in added_series_ids:
                continue

            air_date_str = ep.get("airDateUtc")
            if not air_date_str:
                continue

            air_date_utc = datetime.strptime(air_date_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
            air_date_pt = air_date_utc.astimezone(ZoneInfo("America/Los_Angeles"))
            days_left = max(0, (air_date_pt.date() - now_pt.date()).days)
            d_str = "Day" if days_left == 1 else "Days"
            
            season_num = ep.get("seasonNumber")
            ep_num = ep.get("episodeNumber")

            if season_num == 1 and ep_num == 1:
                tagline = f"SERIES PREMIERE in {days_left} {d_str}"
            elif ep_num == 1 and season_num > 1:
                tagline = f"SEASON {season_num} Returns in {days_left} {d_str}"
            else:
                try:
                    eps_url = f"{sonarr_url}/api/v3/episode?seriesId={series_id}"
                    series_eps = requests.get(eps_url, headers=headers, timeout=5).json()
                    
                    prev_air_date_pt = None
                    for e in series_eps:
                        if e.get("seasonNumber") == season_num and e.get("episodeNumber") == (ep_num - 1):
                            prev_air_str = e.get("airDateUtc")
                            if prev_air_str:
                                p_utc = datetime.strptime(prev_air_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                                prev_air_date_pt = p_utc.astimezone(ZoneInfo("America/Los_Angeles"))
                            break
                    
                    if prev_air_date_pt:
                        gap = (air_date_pt.date() - prev_air_date_pt.date()).days
                        if gap <= min_gap:
                            continue
                        else:
                            tagline = f"RETURNS in {days_left} {d_str}"
                    else:
                        continue
                        
                except Exception as e:
                    sys_logger.error(f"Failed to check previous episode gap for {title}: {e}")
                    continue

            poster_url = None
            for img in series.get("images", []):
                if img.get("coverType") == "poster":
                    poster_url = img.get("remoteUrl")
                    if not poster_url:
                        local_url = img.get("url")
                        if local_url:
                            sep = "&" if "?" in local_url else "?"
                            poster_url = f"{sonarr_url}{local_url}{sep}apikey={sonarr_key}"

            if not poster_url:
                continue

            upcoming.append({
                "title": title,
                "tvdb_id": series.get("tvdbId"),
                "year": series.get("year"),
                "type": "show",
                "days_left": days_left,
                "tagline": tagline,
                "season": season_num,
                "episode": ep_num,
                "poster_url": poster_url
            })
            
            added_series_ids.add(series_id)

        upcoming.sort(key=lambda x: x["days_left"])
        return upcoming[:max_items]

    except Exception as e:
        sys_logger.error(f"Sonarr Scraper Crash: {e}")
        return []
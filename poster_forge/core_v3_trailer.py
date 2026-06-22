import os
import time
import shutil
import yt_dlp
import re
from datetime import datetime
from config import load_settings
from logger import sys_logger
from plexapi.server import PlexServer
from core_v3_overlay import create_and_upload_overlay
from core_plex import translate_path

def download_and_inject_trailer(title, tmdb_id, tagline, poster_url, year=None, position=None, bg_color=None, transparency=None, font_name=None, font_color=None):
    sys_logger.info(f"🎬 Starting Path A (Trailer Download) for: {title}")
    settings = load_settings()

    # 1. Connect to Plex
    try:
        plex = PlexServer(settings.get("PLEX_URL"), settings.get("PLEX_TOKEN"))
        target_section = None
        lib_path = None
        for section in plex.library.sections():
            if "coming soon" in section.title.lower():
                target_section = section
                lib_path = translate_path(section.locations[0])
                break

        if not lib_path:
            sys_logger.error("Could not find a library named 'Coming Soon' with a valid folder path.")
            return False
    except Exception as e:
        sys_logger.error(f"Plex Connection Error: {e}")
        return False

    # 2. Formulate Paths
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()

    # THE FIX: Guarantee a year exists in the filename to satisfy Plex Smart Collections
    if year and str(year) not in safe_title:
        safe_title = f"{safe_title} ({year})"
    elif not re.search(r'\(\d{4}\)', safe_title):
        current_year = datetime.now().year
        safe_title = f"{safe_title} ({current_year})"

    # (Keep your existing base_name logic below)
    base_name = f"{safe_title} {{tvdb-{tmdb_id}}}" if "PREMIERE" in tagline.upper() else f"{safe_title} {{tmdb-{tmdb_id}}}"
    
    final_filename = f"{base_name}.mp4"
    target_dir = os.path.join(lib_path, base_name)
    final_file_path = os.path.join(target_dir, final_filename)

    if os.path.exists(final_file_path):
        sys_logger.info(f"Trailer already exists on disk: {final_filename}")
        return True

    temp_file_path = f"/tmp/{final_filename}"

    temp_file_path = f"/tmp/{final_filename}"

    # 3. Search YouTube & Download
    sys_logger.info(f"🔍 Searching YouTube for: {title} official trailer")
    ydl_opts = {
        'format': 'bestvideo[ext=mp4][height<=2160]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': temp_file_path,
        'quiet': False,
        'verbose': True,
        'no_warnings': False,
        'match_filter': lambda info, *args, **kwargs: 'Video too long' if info.get('duration', 0) > 360 else None
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_query = f"ytsearch1:{title} official trailer"
            ydl.extract_info(search_query, download=True)
    except Exception as e:
        error_msg = str(e)
        # THE FIX: Catch the Premiere Edge Case gracefully
        if "Premieres in" in error_msg:
            sys_logger.warning(f"YouTube Trailer for '{title}' is a scheduled Premiere and hasn't aired yet! Skipping.")
        else:
            sys_logger.error(f"yt-dlp Crash for {title}: {error_msg}")
        return False

    # 4. Move to NAS
    try:
        # THE FIX: Ignore SMB ghost folder locks
        try:
            os.makedirs(target_dir, exist_ok=True)
        except FileExistsError:
            pass 
        
        sys_logger.info(f"🚚 Copying finished trailer to NAS: {final_file_path}")
        # THE FIX: We must use copyfile, NOT copy2. copy2 attempts to preserve Linux metadata/permissions,
        # which completely overwrites and breaks Synology NAS ACLs when mounted via macOS SMB!
        shutil.copyfile(temp_file_path, final_file_path)
        os.remove(temp_file_path)
    except Exception as e:
        sys_logger.error(f"Failed to move file to NAS: {e}")
        return False

    # 5. Trigger Plex Scan
    sys_logger.info("Triggering Plex 'Coming Soon' Library Scan...")
    target_section.update()

    # 6. Wait for Plex
    target_item = None
    sys_logger.info(f"Waiting for Plex to process the exact file path...")
    
    for _ in range(12):
        time.sleep(5)
        for item in target_section.search(sort="addedAt:desc", limit=20):
            for media in item.media:
                for part in media.parts:
                    if final_filename in part.file:
                        target_item = item
                        break
                if target_item: break
            if target_item: break
        if target_item: break

    if not target_item:
        sys_logger.error(f"Plex scanned, but could not find the file in the database yet: {final_filename}")
        return False

    # 7. Force Rename and Lock
    try:
        sys_logger.info(f"Locking Plex title to '{title}'.")
        target_item.edit(**{"title.value": title, "title.locked": 1})
    except Exception as e:
        pass

    # 8. Apply Banner
    sys_logger.info(f"Painting custom banner for: {title}")
    success = create_and_upload_overlay(target_item, poster_url, tagline, position, bg_color, transparency, font_name, font_color)

    # 9. Immediately add to Collection so the user doesn't have to wait for the nightly run!
    try:
        target_item.addCollection(["Coming Soon"])
        sys_logger.info(f"Successfully added '{title}' to the 'Coming Soon' collection.")
    except Exception as e:
        sys_logger.error(f"Failed to add '{title}' to the 'Coming Soon' collection: {e}")

    return success
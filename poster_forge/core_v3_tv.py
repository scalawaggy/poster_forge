from config import load_settings
from logger import sys_logger
from plexapi.server import PlexServer
from core_v3_overlay import create_and_upload_overlay

def process_returning_show(title, tmdb_id, tagline, poster_url, position=None, bg_color=None, transparency=None, font_name=None, font_color=None):
    """Path B: Finds an existing TV show, paints the custom banner, and tags it for the home screen."""
    settings = load_settings()
    
    try:
        plex = PlexServer(settings.get("PLEX_URL"), settings.get("PLEX_TOKEN"))
        target_show = None
        
        # 1. Search all TV libraries for the existing show
        for section in plex.library.sections():
            if section.type == 'show':
                results = section.search(title=title)
                if results:
                    target_show = results[0]
                    break
                    
        if not target_show:
            sys_logger.error(f"Path B Error: Could not find a TV show named '{title}' in Plex.")
            return False
            
        # 2. Paint the custom "Returns in X Days" banner
        sys_logger.info(f"Painting 'Returning Soon' banner for existing show: {target_show.title}")
        create_and_upload_overlay(target_show, poster_url, tagline, position, bg_color, transparency, font_name, font_color)
        
        # 3. Add the Smart Collection Tag to push it to the Home Screen
        sys_logger.info(f"Tagging '{target_show.title}' with 'Returning Soon' collection...")
        target_show.addCollection("Returning Soon")
        
        return True
        
    except Exception as e:
        sys_logger.error(f"Path B TV Show Error for {title}: {e}")
        return False
import sys
from plexapi.server import PlexServer
from config import load_settings

settings = load_settings()
plex = PlexServer(settings.get("PLEX_URL"), settings.get("PLEX_TOKEN"))

for section in plex.library.sections():
    if "coming soon" in section.title.lower():
        items = section.search(title="The Get Out")
        if items:
            item = items[0]
            print(f"Found: {item.title}")
            print(f"Current Release Date: {item.originallyAvailableAt}")
            try:
                item.edit(**{"originallyAvailableAt.value": "2026-06-25", "originallyAvailableAt.locked": 1})
                print("Edit successful!")
            except Exception as e:
                print(f"Edit failed: {e}")
            
            # Fetch again
            item.reload()
            print(f"New Release Date: {item.originallyAvailableAt}")

import sys
from config import load_settings
from plexapi.server import PlexServer

settings = load_settings()
plex = PlexServer(settings.get("PLEX_URL"), settings.get("PLEX_TOKEN"))

for section in plex.library.sections():
    if "coming soon" in section.title.lower():
        items = section.search(title="Hold the Fort")
        if items:
            item = items[0]
            print(f"Item: {item.title}")
            print(f"Current Date: {item.originallyAvailableAt}")
            try:
                item.edit(**{"originallyAvailableAt.value": "2026-06-25", "originallyAvailableAt.locked": 1})
                print("Edit Success!")
            except Exception as e:
                print(f"Edit Failed: {e}")

import os
import json

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
os.makedirs(DATA_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')

LOG_DIR = os.environ.get('LOG_DIR', os.path.join(os.path.dirname(__file__), 'logs'))
os.makedirs(LOG_DIR, exist_ok=True)

def load_settings():
    settings = {
        "TMDB_API_KEY": os.environ.get("TMDB_API_KEY", ""),
        "TVDB_API_KEY": os.environ.get("TVDB_API_KEY", ""),
        "PLEX_URL": os.environ.get("PLEX_URL", ""),
        "PLEX_TOKEN": os.environ.get("PLEX_TOKEN", ""),
        "PLEX_BASE_PATH": os.environ.get("PLEX_BASE_PATH", ""),
        "DOCKER_BASE_PATH": os.environ.get("DOCKER_BASE_PATH", ""),
        "DEFAULT_SOURCE_MOVIE": "tmdb",
        "DEFAULT_SOURCE_SHOW": "tvdb",
        "AUTO_BACKGROUNDS": True,
        "SAVE_DISK": False,
        "DEBUG_MODE": False,
        "OVERLAY_FONT": "Roboto-Black.ttf",
        "OVERLAY_POSITION": "bottom",
        "OVERLAY_COLOR": "#c80000",
        "OVERLAY_TRANSPARENCY": 220,
        "OVERLAY_FONT_COLOR": "#ffffff",
        "OVERLAY_FONT_SCALE": 100,
        "WEBHOOK_TOKEN": "",
        
        "RADARR_URL": "",
        "RADARR_API_KEY": "",
        "SONARR_URL": "",
        "SONARR_API_KEY": "",
        "V3_DAYS_OUT": 30,
        "V3_MAX_MOVIES": 15,
        "V3_MAX_SHOWS": 15,
        "V3_MIN_GAP_DAYS": 29,
        "V3_AUTO_ADD": False  # NEW TOGGLE
    }
    
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                saved = json.load(f)
                settings.update(saved)
        except Exception as e:
            print(f"Error reading settings: {e}")
            
    return settings

def save_settings(new_settings):
    current = load_settings()
    current.update(new_settings)
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(current, f, indent=4)
    return current
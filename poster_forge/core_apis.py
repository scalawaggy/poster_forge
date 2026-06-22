import requests
from config import load_settings
from logger import sys_logger

def get_tmdb_collection_id(title):
    settings = load_settings()
    tmdb_key = settings.get("TMDB_API_KEY")
    if not title or not tmdb_key: return None
    try:
        url = f"https://api.themoviedb.org/3/search/collection?api_key={tmdb_key}&query={title}"
        res = requests.get(url, timeout=10).json()
        if res.get('results'):
            return res['results'][0]['id']
    except Exception as e:
        sys_logger.error(f"TMDB Collection Search Error: {e}")
    return None

def get_tmdb_images(tmdb_id, mode='poster', media_type='movie', season_number=None):
    settings = load_settings()
    tmdb_key = settings.get("TMDB_API_KEY")
    if not tmdb_id or not tmdb_key: return []
    try:
        if media_type == 'collection':
            url = f"https://api.themoviedb.org/3/collection/{tmdb_id}/images?api_key={tmdb_key}"
        elif media_type in ['show', 'season'] and season_number is not None:
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}/images?api_key={tmdb_key}"
        elif media_type in ['show', 'season']:
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/images?api_key={tmdb_key}"
        else:
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/images?api_key={tmdb_key}"
            
        res = requests.get(url, timeout=10).json()
        key = 'posters' if mode == 'poster' else 'backdrops'
        images = res.get(key) or []
        
        valid_images = [i for i in images if i.get('iso_639_1') in ['en', None]]
        if mode == 'poster':
            valid_images = [i for i in valid_images if i.get('aspect_ratio', 0) < 0.8]
        else:
            valid_images = [i for i in valid_images if i.get('aspect_ratio', 0) > 1.0]
            
        # Prioritize English over Textless (None), then by vote_average
        sorted_images = sorted(valid_images, key=lambda x: (x.get('iso_639_1') == 'en', x.get('vote_average', 0)), reverse=True)
        return [f"https://image.tmdb.org/t/p/original{i['file_path']}" for i in sorted_images[:16]]
    except Exception as e:
        sys_logger.error(f"TMDB Fetch Error: {e}")
        return []

def get_tvdb_images(tvdb_id, mode='poster', media_type='show', season_number=None):
    settings = load_settings()
    tvdb_key = settings.get("TVDB_API_KEY")
    if not tvdb_id or not tvdb_key: return []
    try:
        login = requests.post("https://api4.thetvdb.com/v4/login", json={"apikey": tvdb_key}).json()
        if 'data' not in login: return []
        headers = {"Authorization": f"Bearer {login['data']['token']}"}

        artworks = []
        default_img = None 

        if media_type == 'movie':
            url = f"https://api4.thetvdb.com/v4/movies/{tvdb_id}/extended"
            res = requests.get(url, headers=headers).json()
            data = res.get('data') or {}
            artworks = data.get('artworks') or data.get('artwork') or []
        elif season_number is not None:
            series_url = f"https://api4.thetvdb.com/v4/series/{tvdb_id}/extended"
            series_res = requests.get(series_url, headers=headers).json()
            series_data = series_res.get('data') or {}
            season_id = None
            
            if 'seasons' in series_data:
                for s in series_data['seasons']:
                    if str(s.get('number')) == str(season_number):
                        season_id = s.get('id')
                        default_img = s.get('image')
                        break
                            
            if season_id:
                art_url = f"https://api4.thetvdb.com/v4/seasons/{season_id}/extended"
                art_res = requests.get(art_url, headers=headers).json()
                art_data = art_res.get('data') or {}
                artworks = art_data.get('artworks') or art_data.get('artwork') or []
        else:
            url = f"https://api4.thetvdb.com/v4/series/{tvdb_id}/extended"
            res = requests.get(url, headers=headers).json()
            data = res.get('data') or {}
            artworks = data.get('artworks') or data.get('artwork') or []

        if media_type == 'movie':
            target_types = [14] if mode == 'poster' else [15]
        elif season_number is not None:
            target_types = [7] if mode == 'poster' else [8]
        else:
            target_types = [2] if mode == 'poster' else [3]

        valid_images = [art for art in artworks if art.get('type') in target_types and art.get('language') in ['eng', None, 'zxx']]
        
        # Prioritize English over Textless (zxx/None), then by score
        sorted_images = sorted(valid_images, key=lambda x: (x.get('language') == 'eng', x.get('score', 0)), reverse=True)
        results = [i['image'] for i in sorted_images[:16]]
        
        if not results and default_img and mode == 'poster':
            results.append(default_img)
            
        return results
    except Exception as e:
        sys_logger.error(f"TVDB Fetch Error: {e}")
        return []
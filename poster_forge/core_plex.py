import os
import requests
from plexapi.server import PlexServer
from config import load_settings
from logger import forge_logger
from core_apis import get_tmdb_images, get_tvdb_images, get_tmdb_collection_id

def get_plex_instance():
    settings = load_settings()
    return PlexServer(settings.get("PLEX_URL"), settings.get("PLEX_TOKEN"))

def unlock_field(item, field):
    try:
        item.edit(**{f"{field}.locked": 0})
        item.reload()
    except Exception as e:
        forge_logger.info(f"Warning: Could not unlock {field} - {e}")

def translate_path(plex_path):
    settings = load_settings()
    p_base = settings.get("PLEX_BASE_PATH", "")
    d_base = settings.get("DOCKER_BASE_PATH", "")
    
    if not plex_path: return ""
    clean_plex = plex_path.replace('\\', '/')
    if p_base and clean_plex.startswith(p_base):
        clean_plex = clean_plex.replace(p_base, '', 1)
    clean_plex = clean_plex.lstrip('/')
    return os.path.join(d_base, clean_plex)

def download_to_disk(url, target_dir, filename):
    if not os.path.exists(target_dir): return False
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            with open(os.path.join(target_dir, filename), 'wb') as f:
                f.write(res.content)
            return True
    except: pass
    return False

def restore_clean_poster(item):
    """Fetches the best clean poster from TMDB/TVDB and applies it."""
    try:
        tmdb_id, tvdb_id = None, None
        if hasattr(item, 'guids'):
            for guid in item.guids:
                if 'tmdb://' in guid.id: tmdb_id = guid.id.replace('tmdb://', '').split('/')[-1]
                if 'tvdb://' in guid.id: tvdb_id = guid.id.replace('tvdb://', '').split('/')[-1]

        settings = load_settings()
        default_source = settings.get("DEFAULT_SOURCE_SHOW", "tvdb")
        
        best_url = None
        if default_source == 'tvdb' and tvdb_id:
            imgs = get_tvdb_images(tvdb_id, 'poster', item.type)
            if imgs: best_url = imgs[0]
        if not best_url and tmdb_id:
            imgs = get_tmdb_images(tmdb_id, 'poster', item.type)
            if imgs: best_url = imgs[0]
        if not best_url and default_source != 'tvdb' and tvdb_id:
            imgs = get_tvdb_images(tvdb_id, 'poster', item.type)
            if imgs: best_url = imgs[0]
            
        if best_url:
            unlock_field(item, 'thumb')
            item.uploadPoster(url=best_url)
            return True
    except Exception as e:
        forge_logger.info(f"Warning: Could not restore clean poster - {e}")
    return False

def process_payload(payload):
    settings = load_settings()
    
    rating_key = payload.get('ratingKey')
    target_type = payload.get('type') 
    all_seasons = payload.get('allSeasons', False)
    seasons_only = payload.get('seasonsOnly', False)
    season_num = payload.get('seasonNum')
    manual_url = payload.get('manualUrl')
    
    art_mode = payload.get('artMode', 'poster') 
    target_art = payload.get('targetArt', 'both') 

    # FIX: Allow manual UI actions to override the global setting
    payload_save_disk = payload.get('saveDisk')
    save_disk = payload_save_disk if payload_save_disk is not None else settings.get("SAVE_DISK", False)
    apply_backgrounds = settings.get("AUTO_BACKGROUNDS", True)

    if target_type == 'season' and not all_seasons:
        seasons_only = True

    logs = []
    new_urls = {}
    stats = {
        "series": 0,
        "seasons": 0,
        "backgrounds": 0,
        "fallbacks": 0,
        "failures": 0
    }

    try:
        plex = get_plex_instance()
        item = plex.fetchItem(int(rating_key))
        
        if item.type == 'movie':
            default_source = settings.get("DEFAULT_SOURCE_MOVIE", "tmdb")
        else:
            default_source = settings.get("DEFAULT_SOURCE_SHOW", "tvdb")
        
        action_name = "MANUAL OVERRIDE" if manual_url else "AUTO-APPLY"
        logs.append(f"--- {action_name}: {item.title} ---")

        tmdb_id, tvdb_id = None, None
        if hasattr(item, 'guids'):
            for guid in item.guids:
                if 'tmdb://' in guid.id: tmdb_id = guid.id.replace('tmdb://', '').split('/')[-1]
                if 'tvdb://' in guid.id: tvdb_id = guid.id.replace('tvdb://', '').split('/')[-1]

        if item.type == 'collection' and not tmdb_id:
            tmdb_id = get_tmdb_collection_id(item.title)

        def apply_art(target_item, best_url, is_main=True, mode='poster'):
            if not best_url: return False
            field = 'thumb' if mode == 'poster' else 'art'
            unlock_field(target_item, field)
            
            if mode == 'poster': target_item.uploadPoster(url=best_url)
            else: target_item.uploadArt(url=best_url)
            
            if save_disk:
                if item.type == 'collection': return True
                dir_path = ""
                if item.type == 'movie':
                    try: dir_path = os.path.dirname(translate_path(item.media[0].parts[0].file))
                    except: pass
                else:
                    dir_path = translate_path(item.locations[0] if item.locations else "")

                if dir_path:
                    fname = "poster.jpg" if mode == 'poster' else "background.jpg"
                    if not is_main: fname = f"season{target_item.index:02d}-{fname}"
                    download_to_disk(best_url, dir_path, fname)
            return True

        def get_best_url(req_type, s_num=None, mode='poster'):
            if default_source == 'tvdb':
                if tvdb_id:
                    imgs = get_tvdb_images(tvdb_id, mode, req_type, s_num)
                    if imgs: return imgs[0], 'TVDB'
                if tmdb_id:
                    imgs = get_tmdb_images(tmdb_id, mode, req_type, s_num)
                    if imgs: 
                        stats['fallbacks'] += 1
                        return imgs[0], 'TMDB (Fallback)'
            else:
                if tmdb_id:
                    imgs = get_tmdb_images(tmdb_id, mode, req_type, s_num)
                    if imgs: return imgs[0], 'TMDB'
                if tvdb_id:
                    imgs = get_tvdb_images(tvdb_id, mode, req_type, s_num)
                    if imgs: 
                        stats['fallbacks'] += 1
                        return imgs[0], 'TVDB (Fallback)'
            
            stats['failures'] += 1
            return None, 'None'

        if not seasons_only:
            if manual_url:
                if apply_art(item, manual_url, is_main=True, mode=art_mode):
                    logs.append(f"Main {art_mode.capitalize()} (Manual): Updated")
                    new_urls['main'] = manual_url
            else:
                if target_art in ['poster', 'both']:
                    t_url, t_src = get_best_url(item.type, mode='poster')
                    if apply_art(item, t_url, is_main=True, mode='poster'):
                        logs.append(f"Main Poster ({t_src}): Updated")
                        new_urls['main'] = t_url
                        stats['series'] += 1
                    else: logs.append("[SKIP] Main Poster")
                
                if target_art in ['backdrop', 'both'] and apply_backgrounds:
                    b_url, b_src = get_best_url(item.type, mode='backdrop')
                    if apply_art(item, b_url, is_main=True, mode='backdrop'):
                        logs.append(f"Main Background ({b_src}): Updated")
                        new_urls['main'] = b_url
                        stats['backgrounds'] += 1
                    else: logs.append("[SKIP] Main Background")

        if item.type == 'show' and (all_seasons or seasons_only or season_num is not None):
            for season in item.seasons():
                if season.index < 0: continue
                if season_num is not None and str(season.index) != str(season_num): continue

                if manual_url and str(season.index) == str(season_num):
                    if apply_art(season, manual_url, is_main=False, mode=art_mode):
                        logs.append(f"Season {season.index} {art_mode.capitalize()} (Manual): Updated")
                        new_urls[str(season.index)] = manual_url
                elif not manual_url:
                    if target_art in ['poster', 'both']:
                        s_url, s_src = get_best_url('season', season.index, mode='poster')
                        if apply_art(season, s_url, is_main=False, mode='poster'):
                            logs.append(f"Season {season.index} Poster ({s_src}): Updated")
                            new_urls[str(season.index)] = s_url
                            stats['seasons'] += 1
                        else: logs.append(f"[SKIP] Season {season.index} Poster")



    except Exception as e:
        logs.append(f"Error: {str(e)}")
        stats['failures'] += 1
    finally:
        full_log = "\n".join(logs)
        forge_logger.info(full_log)

    return full_log, new_urls, stats
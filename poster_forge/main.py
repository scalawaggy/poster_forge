from fastapi import FastAPI, Request, File, UploadFile, Form, BackgroundTasks
from fastapi import Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from io import BytesIO
from PIL import Image
import os
from datetime import datetime
import asyncio
from datetime import timedelta
import requests
from plexapi.server import PlexServer

from config import load_settings, save_settings
from core_plex import get_plex_instance, unlock_field, process_payload, translate_path
from core_apis import get_tmdb_images, get_tvdb_images, get_tmdb_collection_id
from logger import sys_logger, wh_logger, forge_logger
from core_v3_automation import update_v3_banners

app = FastAPI(docs_url=None, redoc_url=None)

# Mount static files (favicon, etc)
import os
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")
tautulli_cache = {}

import ipaddress
import hashlib
from fastapi.responses import RedirectResponse

def get_session_token(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

@app.middleware("http")
async def cookie_auth_middleware(request: Request, call_next):
    # Skip auth for static files, webhooks, and the API payload processor
    path = request.url.path
    if path.startswith("/static") or path.startswith("/api/webhook/tautulli") or path in ["/login", "/logout"]:
        return await call_next(request)
        
    settings = load_settings()
    auth_user = settings.get("AUTH_USER", "")
    auth_pass = settings.get("AUTH_PASS", "")
    auth_bypass = str(settings.get("AUTH_BYPASS_LOCAL", "true")).lower() == "true"
    
    if not auth_user or not auth_pass:
        return await call_next(request)
        
    if auth_bypass:
        client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "127.0.0.1").split(",")[0].strip()
        try:
            if ipaddress.ip_address(client_ip).is_private:
                return await call_next(request)
        except ValueError:
            pass
            
    expected_token = get_session_token(auth_pass)
    if request.cookies.get("forge_session") == expected_token:
        return await call_next(request)
            
    if path.startswith("/api/"):
        return JSONResponse(content={"error": "Unauthorized", "message": "You must be logged in."}, status_code=401)
        
    return RedirectResponse(url="/login", status_code=303)

sys_logger.info("Poster Forge V2 Server Started.")

async def run_daily_scheduler():
    """Calculates time until scheduled maintenance, sleeps in chunks to detect changes, and loops forever."""
    while True:
        settings = load_settings()
        
        # Check if automated maintenance is enabled
        is_enabled = str(settings.get("ENABLE_MAINTENANCE", "true")).lower() == "true"
        if not is_enabled:
            await asyncio.sleep(60)
            continue
            
        time_str = settings.get("MAINTENANCE_TIME", "03:00")
        
        try:
            target_hour, target_minute = map(int, time_str.split(":"))
        except:
            target_hour, target_minute = 3, 0

        now = datetime.now()
        target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        
        if target <= now:
            target += timedelta(days=1)
        
        wait_seconds = (target - now).total_seconds()
        time_fmt = target.strftime("%I:%M %p")
        sys_logger.info(f"Next V3 Auto-Pilot scheduled for {time_fmt} (in {wait_seconds/3600:.1f} hours).")
        
        # Sleep in small 60-second chunks so we can detect if you change the time in Settings
        slept = 0
        time_changed = False
        while slept < wait_seconds:
            await asyncio.sleep(60)
            slept += 60
            
            # Check if setting changed while we were sleeping
            s = load_settings()
            current_time_str = s.get("MAINTENANCE_TIME", "03:00")
            current_is_enabled = str(s.get("ENABLE_MAINTENANCE", "true")).lower() == "true"
            
            if current_time_str != time_str or not current_is_enabled:
                if current_time_str != time_str:
                    sys_logger.info(f"Auto-Pilot time changed to {current_time_str}! Recalculating schedule...")
                time_changed = True
                break
                
        # If the timer naturally finished without the settings changing, run the Janitor!
        if not time_changed:
            try:
                sys_logger.info(f"Running scheduled Auto-Pilot at {time_fmt}...")
                await asyncio.to_thread(update_v3_banners)
            except Exception as e:
                sys_logger.error(f"Auto-Pilot Crash: {e}")


# THE SEMAPHORE BOUNCER & QUEUE TRACKER
v3_semaphore = None 
v3_active_tasks = 0 

async def v3_worker_wrapper(title, tmdb_id, tagline, poster_url, is_returning_show, year=None, position=None, bg_color=None, transparency=None, font_name=None, font_color=None):
    """Wraps the heavy tasks in a lock so they don't crash the server."""
    global v3_semaphore, v3_active_tasks
    sys_logger.info(f"'{title}' is in the waiting line...")
    
    # Wait for the bouncer to let this task in
    async with v3_semaphore:
        sys_logger.info(f"Starting extraction for '{title}'...")
        try:
            if is_returning_show:
                from core_v3_tv import process_returning_show
                result = await asyncio.to_thread(process_returning_show, title, tmdb_id, tagline, poster_url, position, bg_color, transparency, font_name, font_color)
            else:
                from core_v3_trailer import download_and_inject_trailer
                result = await asyncio.to_thread(download_and_inject_trailer, title, tmdb_id, tagline, poster_url, year, position, bg_color, transparency, font_name, font_color)
                
            if result:
                sys_logger.info(f"Completed '{title}' perfectly.")
            else:
                sys_logger.warning(f"Task finished with errors: '{title}' failed to process.")
        except Exception as e:
            sys_logger.error(f"Worker Crash on '{title}': {str(e)}")
            
    # ONLY DECREMENT: Task is totally done, remove it from the counter
    v3_active_tasks -= 1 

@app.on_event("startup")
async def startup_event():
    """Fires exactly once when the Docker container boots up."""
    global v3_semaphore
    v3_semaphore = asyncio.Semaphore(1) # Only 1 task allowed at a time
    asyncio.create_task(run_daily_scheduler())

@app.get("/login", response_class=HTMLResponse)
async def page_login(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")

@app.post("/login")
async def post_login(request: Request, username: str = Form(""), password: str = Form("")):
    settings = load_settings()
    auth_user = settings.get("AUTH_USER", "")
    auth_pass = settings.get("AUTH_PASS", "")
    
    if auth_user and auth_pass and username == auth_user and password == auth_pass:
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(key="forge_session", value=get_session_token(auth_pass), httponly=True, max_age=86400*30)
        return response
        
    return templates.TemplateResponse(request=request, name="login.html", context={"error": "Invalid username or password"})

@app.get("/logout")
async def get_logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("forge_session")
    return response

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/about", response_class=HTMLResponse)
async def page_about(request: Request):
    return templates.TemplateResponse(request=request, name="about.html")

@app.get("/settings", response_class=HTMLResponse)
async def serve_settings_page(request: Request):
    return templates.TemplateResponse(request=request, name="settings.html")

@app.get("/api/settings")
async def api_get_settings():
    return JSONResponse(content={"success": True, "settings": load_settings()})

@app.post("/api/imagemaid/run")
async def imagemaid_run(request: Request, background_tasks: BackgroundTasks):
    try:
        settings = load_settings()
        plex_url = settings.get('PLEX_URL', '')
        plex_token = settings.get('PLEX_TOKEN', '')
        
        if not plex_url or not plex_token:
            return JSONResponse(content={"success": False, "message": "Plex URL and Token not configured."})

        import subprocess
        
        def run_script():
            forge_logger.info("--- IMAGEMAID CLEANUP INITIATED ---")
            try:
                cmd = [
                    "python3", "/app/ImageMaid/imagemaid.py", 
                    "-u", plex_url, 
                    "-t", plex_token, 
                    "-p", "/plex", 
                    "-m", "remove",
                    "-et", "-cb", "-od", "-pt", "-i"
                ]
                with open("logs/imagemaid.log", "w", encoding="utf-8") as f:
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
                    for line in process.stdout:
                        f.write(line)
                        f.flush()
                        forge_logger.info(line.strip())
                    process.wait()
                forge_logger.info("--- IMAGEMAID CLEANUP COMPLETE ---")
            except Exception as e:
                forge_logger.error(f"ImageMaid execution error: {e}")

        background_tasks.add_task(run_script)
        return JSONResponse(content={"success": True, "message": "ImageMaid Cleanup Started! Check the logs."})
        
    except Exception as e:
        sys_logger.error(f"ImageMaid Setup Error: {e}")
        return JSONResponse(content={"success": False, "message": str(e)})

@app.get("/api/imagemaid/logs")
async def api_imagemaid_logs():
    try:
        log_path = "logs/imagemaid.log"
        if not os.path.exists(log_path):
            return JSONResponse({"success": True, "logs": "Initializing ImageMaid... Please wait."})
            
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-200:]
        return JSONResponse({"success": True, "logs": "".join(lines)})
    except Exception as e:
        return JSONResponse({"success": False, "logs": f"Error reading logs: {e}"})

@app.post("/api/settings")
async def api_post_settings(request: Request):
    try:
        new_settings = await request.json()
        updated = save_settings(new_settings)
        sys_logger.info("Settings updated via GUI.")
        return JSONResponse(content={"success": True, "message": "Settings Saved!", "settings": updated})
    except Exception as e:
        sys_logger.error(f"Settings Save Error: {e}")
        return JSONResponse(content={"success": False, "message": str(e)})

@app.get("/api/libraries")
async def api_get_libraries():
    try:
        plex = get_plex_instance()
        libs = [{"id": sec.key, "title": sec.title, "type": sec.type} for sec in plex.library.sections() if sec.type in ['movie', 'show']]
        return JSONResponse(content={"success": True, "libraries": libs})
    except Exception as e:
        sys_logger.error(f"Library Fetch Error: {e}")
        return JSONResponse(content={"success": False, "error": str(e)})

@app.get("/api/search")
async def api_search(q: str = '', libraryId: str = '', page: int = 1, limit: int = 50, sort: str = 'titleSort:asc', isCollection: str = 'false'):
    try:
        plex = get_plex_instance()
        section = plex.library.sectionByID(int(libraryId))
        
        kwargs = {}
        if isCollection == 'true' and 'originallyAvailableAt' in sort: kwargs['sort'] = 'titleSort:asc'
        else: kwargs['sort'] = sort
        if q: kwargs['title__icontains'] = q
        if isCollection == 'true': kwargs['libtype'] = 'collection'
            
        all_items = section.search(**kwargs)
        total = len(all_items)
        start = (page - 1) * limit
        end = start + limit
        paged_items = all_items[start:end]
        
        results = [{
            "ratingKey": item.ratingKey, 
            "title": item.title, 
            "year": getattr(item, 'year', ''), 
            "thumb": item.thumbUrl if getattr(item, 'thumb', None) else "",
            "art": item.artUrl if getattr(item, 'art', None) else ""
        } for item in paged_items]
        
        return JSONResponse(content={"success": True, "results": results, "pagination": {"page": page, "totalPages": (total + limit - 1) // limit, "totalItems": total}})
    except Exception as e:
        sys_logger.error(f"Search API Error: {e}")
        return JSONResponse(content={"success": False, "error": str(e)})

@app.get("/api/library_keys")
async def api_library_keys(libraryId: str, isCollection: str = 'false'):
    try:
        plex = get_plex_instance()
        section = plex.library.sectionByID(int(libraryId))
        if isCollection == 'true': items = [{"ratingKey": i.ratingKey, "title": i.title, "type": "collection"} for i in section.search(libtype='collection')]
        else: items = [{"ratingKey": i.ratingKey, "title": i.title, "type": i.type} for i in section.search()]
        return JSONResponse(content={"success": True, "items": items})
    except Exception as e:
        sys_logger.error(f"Library Keys Error: {e}")
        return JSONResponse(content={"success": False, "error": str(e)})

@app.post("/api/preview")
async def api_preview(request: Request):
    try:
        payload = await request.json()
        ratingKey = payload.get('ratingKey')
        req_type = payload.get('type')
        seasonNum = payload.get('seasonNum')
        artMode = payload.get('artMode', 'poster')
        
        settings = load_settings()
        fallback_source = settings.get("DEFAULT_SOURCE_MOVIE", "tmdb") if req_type == 'movie' else settings.get("DEFAULT_SOURCE_SHOW", "tvdb")
        source = payload.get('source', fallback_source)
        
        plex = get_plex_instance()
        item = plex.fetchItem(int(ratingKey))
        
        tmdb_id, tvdb_id = None, None
        if hasattr(item, 'guids'):
            for guid in item.guids:
                if 'tmdb://' in guid.id: tmdb_id = guid.id.replace('tmdb://', '').split('/')[-1] 
                if 'tvdb://' in guid.id: tvdb_id = guid.id.replace('tvdb://', '').split('/')[-1]
                
        if req_type == 'collection' and not tmdb_id:
            tmdb_id = get_tmdb_collection_id(item.title)
            
        posters = []
        if source == 'tmdb' and tmdb_id:
            posters = get_tmdb_images(tmdb_id, artMode, req_type, seasonNum)
        elif source == 'tvdb' and tvdb_id:
            posters = get_tvdb_images(tvdb_id, artMode, req_type, seasonNum)
            
        return JSONResponse(content={"success": True, "posters": posters})
    except Exception as e:
        sys_logger.error(f"Preview API Error: {e}")
        return JSONResponse(content={"success": False, "error": str(e)})

@app.get("/api/seasons")
async def api_seasons(ratingKey: str):
    try:
        plex = get_plex_instance()
        show = plex.fetchItem(int(ratingKey))
        seasons = [{"seasonNumber": s.index, "title": s.title, "thumb": s.thumbUrl if getattr(s, 'thumb', None) else "", "art": s.artUrl if getattr(s, 'art', None) else ""} for s in show.seasons() if s.index >= 0]
        return JSONResponse(content={"success": True, "seasons": seasons})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})

@app.post("/api/audit/save")
async def api_audit_save(request: Request):
    try:
        import datetime
        payload = await request.json()
        library_title = payload.get('library', 'Unknown Library')
        stats = payload.get('stats', {})
        totals = stats.get('totals', {})
        
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = "".join(c if c.isalnum() else "_" for c in library_title)
        filename = f"logs/audit_{safe_title}_{ts}.txt"
        
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"BATCH PROCESSING AUDIT REPORT\n")
            f.write(f"=============================\n")
            f.write(f"Library: {library_title}\n")
            f.write(f"Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Processing Time: {totals.get('time', 0)} seconds\n\n")
            
            f.write(f"--- SUMMARY ---\n")
            f.write(f"Total Replaced: {totals.get('series',0) + totals.get('seasons',0) + totals.get('backgrounds',0)}\n")
            f.write(f"Series/Movie Posters: {totals.get('series',0)}\n")
            f.write(f"Season Posters: {totals.get('seasons',0)}\n")
            f.write(f"Backgrounds: {totals.get('backgrounds',0)}\n")
            f.write(f"Fallbacks Used: {totals.get('fallbacks',0)}\n")
            
            total_failed = totals.get('failures', 0)
            for cat in ['movie', 'show', 'collection']:
                total_failed += len(stats.get(cat, {}).get('failed', []))
            f.write(f"Failures: {total_failed}\n\n")
            
            f.write(f"--- FAILED ITEMS ---\n")
            if total_failed == 0:
                f.write("None. Perfect Run.\n")
            else:
                for cat in ['movie', 'show', 'collection']:
                    failed_items = stats.get(cat, {}).get('failed', [])
                    for item in failed_items:
                        f.write(f"[{cat.upper()}] {item}\n")
            
        forge_logger.info(f"--- BATCH COMPLETION SUMMARY LOGGED TO: {filename} ---")
        return JSONResponse(content={"success": True, "filepath": filename})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})

@app.post("/api/upload_custom")
async def upload_custom(ratingKey: str = Form(...), targetField: str = Form("thumb"), mediaType: str = Form("movie"), seasonNum: str = Form(None), saveDisk: bool = Form(False), x: float = Form(...), y: float = Form(...), width: float = Form(...), height: float = Form(...), file: UploadFile = File(...)):
    try:
        plex = get_plex_instance()
        item = plex.fetchItem(int(ratingKey))
        
        if mediaType == 'season' and seasonNum and seasonNum != "null":
            for s in item.seasons():
                if str(s.index) == str(seasonNum):
                    item = s
                    break
        
        image_data = await file.read()
        img = Image.open(BytesIO(image_data))
        cropped_img = img.crop((x, y, x + width, y + height))
        
        img_byte_arr = BytesIO()
        cropped_img.save(img_byte_arr, format='JPEG', quality=90)
        img_byte_arr.seek(0)
        
        unlock_field(item, targetField)
        if targetField == 'thumb': item.uploadPoster(filepath=img_byte_arr)
        else: item.uploadArt(filepath=img_byte_arr)

        if saveDisk and mediaType != 'collection':
            dir_path = ""
            if mediaType == 'movie':
                try: dir_path = os.path.dirname(translate_path(item.media[0].parts[0].file))
                except: pass
            else:
                dir_path = translate_path(item.locations[0] if item.locations else "")

            if dir_path and os.path.exists(dir_path):
                fname = "background.jpg" if targetField == 'art' else "poster.jpg"
                if mediaType == 'season': fname = f"season{item.index:02d}-{fname}"
                with open(os.path.join(dir_path, fname), 'wb') as f:
                    f.write(img_byte_arr.getvalue())
                forge_logger.info(f"Local Crop Saved to disk: {os.path.join(dir_path, fname)}")
            
        forge_logger.info(f"Manual Custom Upload: {item.title} ({targetField})")
        return JSONResponse(content={"success": True, "message": f"Custom {targetField} applied!"})
    except Exception as e:
        sys_logger.error(f"Custom Upload Error: {e}")
        return JSONResponse(content={"success": False, "message": str(e)})

@app.post("/api/apply")
async def apply_media(request: Request):
    payload = await request.json()
    result_log, new_urls, stats = process_payload(payload)
    return JSONResponse(content={"success": True, "message": result_log, "newPosterUrls": new_urls, "stats": stats})
@app.post("/api/webhook/tautulli")
async def tautulli_webhook(request: Request, token: str = None, background_tasks: BackgroundTasks = None):
    settings = load_settings()
    configured_token = settings.get("WEBHOOK_TOKEN", "")
    
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        pass
        
    header_token = request.headers.get("token") or request.headers.get("X-Forge-Token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    body_token = payload.get("token") if isinstance(payload, dict) else None
    
    provided_token = token or header_token or body_token
    
    if configured_token and provided_token != configured_token:
        wh_logger.warning(f"Unauthorized webhook attempt with invalid or missing token. Provided: {provided_token}")
        return JSONResponse(content={"success": False, "message": "Unauthorized. Invalid Token."}, status_code=200)
        
    try:
        if not payload:
            payload = await request.json()
        wh_logger.debug(f"Raw incoming payload: {payload}")
        
        rating_key = payload.get('ratingKey', '')
        if rating_key.startswith('{'): rating_key = ''
            
        if not rating_key:
            wh_logger.error(f"Could not extract a valid Plex ID. Payload was: {payload}")
            return JSONResponse(content={"success": False, "message": "No valid ratingKey found in payload."})
            
        plex = get_plex_instance()
        item = plex.fetchItem(int(rating_key))

        # 1. THE V3 GUARDRAIL: Ignore items added directly to Coming Soon
        try:
            if "coming soon" in item.section().title.lower():
                wh_logger.info(f"Webhook Ignored: '{item.title}' belongs to the Coming Soon engine.")
                return JSONResponse(content={"success": True, "message": "Ignored Coming Soon library."})
        except Exception:
            pass
        
        target_season = None
        seasons_only = False
        all_seasons = False
        target_item = item

        # Determine the base title of the real media that just arrived
        if item.type == 'episode':
            show = plex.fetchItem(item.grandparentRatingKey)
            season = plex.fetchItem(item.parentRatingKey)
            
            delta_show = abs((item.addedAt - show.addedAt).total_seconds()) if show.addedAt and item.addedAt else 0
            delta_season = abs((item.addedAt - season.addedAt).total_seconds()) if season.addedAt and item.addedAt else 0
            
            is_new_show = delta_show < 900
            is_new_season = delta_season < 900 or int(item.index) == 1
            
            if not is_new_show and not is_new_season:
                wh_logger.info(f"Webhook Ignored: Episode {item.index} of {show.title} (Season in progress)")
                return JSONResponse(content={"success": True, "message": "Ignored (Episode in progress)"})

            target_key = str(show.ratingKey)
            engine_type = 'show'
            item_title = show.title
            wh_log_title = f"{item_title} (via Episode Drop)"
            target_item = show
            target_season = item.parentIndex
            
            if is_new_show:
                all_seasons = True
            else:
                seasons_only = True

        elif item.type == 'season':
            show = plex.fetchItem(item.parentRatingKey)
            delta_show = abs((item.addedAt - show.addedAt).total_seconds()) if show.addedAt and item.addedAt else 0
            
            is_new_show = delta_show < 900 or int(item.index) == 1
            
            target_key = str(show.ratingKey)
            engine_type = 'show'
            item_title = show.title
            wh_log_title = f"{item_title} (via Season Drop)"
            target_item = show
            target_season = item.index
            
            if is_new_show:
                all_seasons = True
            else:
                seasons_only = True

        else:
            target_key = str(item.ratingKey)
            engine_type = item.type
            item_title = item.title
            wh_log_title = item_title
            all_seasons = True
            
        tmdb_id = None
        if hasattr(target_item, 'guids'):
            for guid in target_item.guids:
                if 'tmdb://' in guid.id:
                    tmdb_id = guid.id.replace('tmdb://', '').split('/')[-1]
                    break

        # 2. THE ASSASSIN & CLEANUP
        try:
            import shutil
            
            # --- 2A: Purge Fake Movie from Coming Soon ---
            for section in plex.library.sections():
                if "coming soon" in section.title.lower():
                    ghosts = section.all()
                    for ghost in ghosts:
                        clean_title = ghost.title.replace(" (Trailer)", "")
                        is_match = False
                        
                        if tmdb_id:
                            is_match = any(f"tmdb://{tmdb_id}" in g.id for g in getattr(ghost, 'guids', []))
                        else:
                            is_match = ghost.title.lower() == item_title.lower() or clean_title.lower() == item_title.lower()
                            
                        if is_match:
                            wh_logger.info(f"ASSASSIN: Real media arrived for '{ghost.title}'. Purging trailer from disk and Plex...")
                            for media in ghost.media:
                                for part in media.parts:
                                    try:
                                        real_path = translate_path(part.file)
                                        dir_to_remove = os.path.dirname(real_path)
                                        if os.path.exists(dir_to_remove):
                                            shutil.rmtree(dir_to_remove)
                                    except Exception as err:
                                        wh_logger.warning(f"Could not delete physical trailer directory: {err}")
                            ghost.delete()
                            break
                            
            # --- 2B: Remove Real Show from "Returning Soon" Collection ---
            if engine_type == 'show':
                # Check Air Date to ensure it's the anticipated episode, not a backfill
                is_anticipated = True
                if item.type == 'episode' and item.originallyAvailableAt:
                    days_since_air = (datetime.now() - item.originallyAvailableAt).days
                    if days_since_air > 14:
                        is_anticipated = False
                        wh_logger.info(f"CLEANUP: Episode {item.index} is a backfill (Aired {days_since_air} days ago). Keeping '{item_title}' in Returning Soon.")
                
                if is_anticipated:
                    try:
                        if any(c.tag == "Returning Soon" for c in target_item.collections):
                            target_item.removeCollection("Returning Soon")
                            wh_logger.info(f"CLEANUP: Removed '{item_title}' from Returning Soon collection.")
                    except Exception as e:
                        wh_logger.warning(f"Failed to remove from Returning Soon: {e}")
                            
        except Exception as e:
            wh_logger.error(f"Assassin Purge failed: {e}")

        # 3. Standard Auto-Apply Logic
        now = datetime.now().timestamp()
        cache_key = f"{target_key}_{target_season}" if engine_type == 'show' else target_key
        
        if cache_key in tautulli_cache and (now - tautulli_cache[cache_key]) < 900:
            wh_logger.info(f"Ignored duplicate webhook for {item_title} Season {target_season} (Debounced)")
            return JSONResponse(content={"success": True, "message": "Ignored (Debounced)"})
            
        tautulli_cache[cache_key] = now
        wh_logger.info(f"Catching webhook for: {wh_log_title}. Offloading to Forge background task...")
        
        engine_payload = {"ratingKey": target_key, "type": engine_type}
        if all_seasons:
            engine_payload["allSeasons"] = True
        elif target_season is not None:
            engine_payload["seasonNum"] = target_season
            if seasons_only:
                engine_payload["seasonsOnly"] = True
            
        background_tasks.add_task(process_payload, engine_payload)
        return JSONResponse(content={"success": True, "message": "Processing in background"})
        
    except Exception as e:
        wh_logger.error(f"Webhook Crash: {str(e)}")
        return JSONResponse(content={"success": False, "message": str(e)})

@app.post("/api/test/connection")
async def test_connection(request: Request):
    """Dynamically tests API keys and URLs before they are saved."""
    try:
        payload = await request.json()
        service = payload.get("service")
        url = payload.get("url", "").rstrip('/')
        api_key = payload.get("api_key", "").strip()

        if service == "tmdb":
            res = requests.get(f"https://api.themoviedb.org/3/configuration?api_key={api_key}", timeout=5)
            if res.status_code == 200: return JSONResponse({"success": True, "message": "TMDB Verified!"})
            else: return JSONResponse({"success": False, "message": f"TMDB Error: {res.status_code}"})

        elif service == "tvdb":
            res = requests.post("https://api4.thetvdb.com/v4/login", json={"apikey": api_key}, timeout=5)
            if res.status_code == 200: return JSONResponse({"success": True, "message": "TVDB Verified!"})
            else: return JSONResponse({"success": False, "message": f"TVDB Error: {res.status_code} (Check Key)"})

        elif service == "plex":
            plex = PlexServer(url, api_key)
            return JSONResponse({"success": True, "message": f"Connected to Plex: {plex.friendlyName}"})

        elif service == "radarr":
            res = requests.get(f"{url}/api/v3/system/status", headers={"X-Api-Key": api_key}, timeout=5)
            if res.status_code == 200: return JSONResponse({"success": True, "message": "Radarr Verified!"})
            else: return JSONResponse({"success": False, "message": f"Radarr Error: {res.status_code}"})

        elif service == "sonarr":
            res = requests.get(f"{url}/api/v3/system/status", headers={"X-Api-Key": api_key}, timeout=5)
            if res.status_code == 200: return JSONResponse({"success": True, "message": "Sonarr Verified!"})
            else: return JSONResponse({"success": False, "message": f"Sonarr Error: {res.status_code}"})

    except Exception as e:
        return JSONResponse({"success": False, "message": f"Connection Failed: Check URL & Host."})

# --- V3 COMING SOON ENGINE ROUTES --- #

@app.get("/api/v3/upcoming")
async def api_v3_upcoming():
    """Queries Radarr/Sonarr for upcoming items."""
    try:
        from core_v3_scraper import get_upcoming_movies, get_upcoming_shows
        movies = get_upcoming_movies()
        shows = get_upcoming_shows()
        return JSONResponse(content={"success": True, "movies": movies, "shows": shows})
    except Exception as e:
        sys_logger.error(f"V3 Scraper API Error: {e}")
        return JSONResponse(content={"success": False, "error": str(e)})

@app.get("/api/v3/fonts")
async def api_v3_fonts():
    """Returns a list of all .ttf files in the data directory."""
    try:
        if not os.path.exists("data"):
            os.makedirs("data")
        fonts = [f for f in os.listdir("data") if f.lower().endswith(".ttf")]
        if "Roboto-Black.ttf" not in fonts:
            fonts.append("Roboto-Black.ttf") # Always include default as fallback
        return JSONResponse(content={"success": True, "fonts": sorted(list(set(fonts)))})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})

@app.get("/api/v3/fetch_posters")
async def api_v3_fetch_posters(tmdb_id: str = None, tvdb_id: str = None, type: str = 'movie'):
    """Fetches top 5 posters from TMDB/TVDB for the picker wizard."""
    try:
        raw_posters = []
        if tmdb_id:
            raw_posters = get_tmdb_images(tmdb_id, 'poster', type, None)[:5]
        elif tvdb_id:
            raw_posters = get_tvdb_images(tvdb_id, 'poster', type, None)[:5]
            
        posters = [{"url": p} for p in raw_posters]
            
        # Return fallback poster if none found
        if not posters:
            posters = [{"url": "https://via.placeholder.com/600x900/111111/444444/?text=No+Poster+Found"}]
            
        return JSONResponse(content={"success": True, "posters": posters})
    except Exception as e:
        return JSONResponse(content={"success": False, "error": str(e)})

@app.post("/api/v3/commit")
async def api_v3_commit(request: Request, background_tasks: BackgroundTasks):
    """Catches items from the UI and hands them to the native background task manager."""
    global v3_active_tasks # BRING IN THE COUNTER
    try:
        payload = await request.json()
        title = payload.get('title')
        tmdb_id = payload.get('tmdb_id')
        tagline = payload.get('tagline')
        poster_url = payload.get('poster_url')
        year = payload.get('year')
        
        # New customization arguments
        position = payload.get('position')
        bg_color = payload.get('bg_color')
        transparency = payload.get('transparency')
        font_name = payload.get('font_name')
        font_color = payload.get('font_color')
        
        is_returning_show = "Returns in" in tagline or "RETURNS in" in tagline
        
        # INCREMENT HERE: Add to the counter the literal millisecond it hits the server
        v3_active_tasks += 1 
        
        # Native FastAPI backgrounding, but passed through our Bouncer
        background_tasks.add_task(v3_worker_wrapper, title, tmdb_id, tagline, poster_url, is_returning_show, year, position, bg_color, transparency, font_name, font_color)
        sys_logger.info(f"Queued: {title}")
            
        return JSONResponse(content={"success": True, "message": f"Queued {title}!"})
    except Exception as e:
        return JSONResponse(content={"success": False, "message": str(e)})

@app.post("/api/v3/preview_overlay")
async def api_preview_overlay(request: Request):
    """Generates the image in RAM and sends it back to the browser for preview."""
    try:
        payload = await request.json()
        poster_url = payload.get('posterUrl')
        tagline = payload.get('tagline', 'COMING SOON')
        position = payload.get('position', 'bottom')
        bg_color = payload.get('bg_color', '#c80000')
        transparency = payload.get('transparency', 220)
        font_name = payload.get('font_name', 'Roboto-Black.ttf')
        font_color = payload.get('font_color', '#ffffff')
        font_scale = payload.get('font_scale', 100)

        from core_v3_overlay import generate_overlay_image
        img_bytes = generate_overlay_image(poster_url, tagline, position=position, bg_color=bg_color, transparency=transparency, font_name=font_name, font_color=font_color, font_scale=font_scale)
        
        return Response(content=img_bytes.getvalue(), media_type="image/jpeg")
    except Exception as e:
        sys_logger.error(f"Preview Generation Error: {e}")
        return JSONResponse(content={"success": False, "error": str(e)})
    
@app.get("/api/v3/status")
async def api_v3_status():
    """Tells the UI exactly how many tasks are left in the worker queue."""
    global v3_active_tasks
    return JSONResponse(content={"success": True, "active_tasks": v3_active_tasks})

@app.post("/api/v3/maintenance/run")
async def api_v3_maintenance_run(background_tasks: BackgroundTasks):
    """Fires the Daily Maintenance routine manually via the Settings UI."""
    try:
        sys_logger.info("Manual V3 Maintenance triggered via Settings UI.")
        # FastAPI's background_tasks will safely run this sync function in a separate thread!
        background_tasks.add_task(update_v3_banners)
        return JSONResponse(content={"success": True, "message": "Janitor started in background! Check terminal logs."})
    except Exception as e:
        return JSONResponse(content={"success": False, "message": str(e)})
    
@app.get("/api/v3/maintenance/logs")
async def api_v3_maintenance_logs():
    """Reads the dedicated Janitor audit trail and sends it to the UI."""
    try:
        log_path = "logs/janitor.log"
        if not os.path.exists(log_path):
            return JSONResponse({"success": True, "logs": "No maintenance has been run yet! The log file is empty."})
            
        with open(log_path, "r", encoding="utf-8") as f:
            # Grab the last 150 lines so the UI doesn't lag if the file gets huge
            lines = f.readlines()[-150:] 
        
        return JSONResponse({"success": True, "logs": "".join(lines)})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})
import requests
import os
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from logger import sys_logger

def hex_to_rgba(hex_color, alpha=255):
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 6:
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4)) + (alpha,)
    return (200, 0, 0, alpha)

def generate_overlay_image(poster_url, tagline, position=None, bg_color=None, transparency=None, font_name=None, font_color=None, font_scale=None):
    from config import load_settings
    settings = load_settings()
    
    position = position or settings.get("OVERLAY_POSITION", "bottom")
    bg_color = bg_color or settings.get("OVERLAY_COLOR", "#c80000")
    transparency = int(transparency) if transparency is not None else int(settings.get("OVERLAY_TRANSPARENCY", 220))
    font_name = font_name or settings.get("OVERLAY_FONT", "Roboto-Black.ttf")
    font_color = font_color or settings.get("OVERLAY_FONT_COLOR", "#ffffff")
    font_scale = int(font_scale) if font_scale is not None else int(settings.get("OVERLAY_FONT_SCALE", 100))
    
    fill_bg = hex_to_rgba(bg_color, transparency)
    fill_text = hex_to_rgba(font_color, 255)
    
    try:
        # Fetch the original poster
        response = requests.get(poster_url, timeout=10)
        base_image = Image.open(BytesIO(response.content)).convert("RGBA")

        # Setup dimensions
        width, height = base_image.size
        # Determine banner position
        banner_height = int(height * 0.11) 
        if position == 'top':
            banner_y = 0
        elif position == 'middle':
            banner_y = (height - banner_height) / 2
        else: # bottom
            banner_y = height - banner_height 

        # Create a blank transparent layer for the banner
        overlay = Image.new('RGBA', base_image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Draw a translucent background
        draw.rectangle([(0, banner_y), (width, banner_y + banner_height)], fill=fill_bg)

        # Merge the translucent banner down onto the main image
        base_image = Image.alpha_composite(base_image, overlay)
        draw = ImageDraw.Draw(base_image)

        # DYNAMIC FONT SCALING FIX
        max_text_width = int(width * 0.90) # Give it a 5% margin on each side
        base_target_size = banner_height * 0.65
        target_font_size = int(base_target_size * (font_scale / 100.0))

        def get_font(size):
            font_path = os.path.join("data", font_name)
            
            if not os.path.exists(font_path) and font_name == "Roboto-Black.ttf":
                try:
                    sys_logger.info(f"Downloading default font {font_name}...")
                    url = "https://raw.githubusercontent.com/googlefonts/roboto/main/src/hinted/Roboto-Black.ttf"
                    r = requests.get(url, timeout=10)
                    os.makedirs("data", exist_ok=True)
                    with open(font_path, "wb") as f:
                        f.write(r.content)
                except Exception as e:
                    sys_logger.error(f"Failed to download font: {e}")

            try:
                if os.path.exists(font_path):
                    return ImageFont.truetype(font_path, size)
                elif os.path.exists(font_name):
                    return ImageFont.truetype(font_name, size)
                else:
                    return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
            except:
                return ImageFont.load_default()

        font = get_font(target_font_size)

        # Calculate exact text width based on the font
        def get_text_width(f, text):
            if hasattr(f, 'getlength'):
                return f.getlength(text)
            else:
                return f.getbbox(text)[2]

        # THE LOOP: Shrink the font 2 pixels at a time until it fits the width!
        while get_text_width(font, tagline) > max_text_width and target_font_size > 10:
            target_font_size -= 2
            font = get_font(target_font_size)

        # PIXEL-PERFECT CENTERING FIX
        # Use textbbox to get the exact rendered pixel boundaries of the text
        bbox = draw.textbbox((0, 0), tagline, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        # Calculate exact top-left coordinate to drop the text so it is mathematically centered
        draw_x = (width - text_w) / 2 - bbox[0]
        draw_y = banner_y + (banner_height - text_h) / 2 - bbox[1]

        draw.text((draw_x, draw_y), tagline, font=font, fill=fill_text)

        # Convert back to standard RGB and save to memory
        img_byte_arr = BytesIO()
        base_image.convert("RGB").save(img_byte_arr, format='JPEG', quality=90)
        img_byte_arr.seek(0)
        
        return img_byte_arr

    except Exception as e:
        sys_logger.error(f"Overlay Generation Error: {e}")
        return BytesIO()

def create_and_upload_overlay(plex_item, poster_url, tagline, position=None, bg_color=None, transparency=None, font_name=None, font_color=None, font_scale=None):
    """Generates the image and pushes it directly to the Plex item."""
    try:
        img_bytes = generate_overlay_image(poster_url, tagline, position, bg_color, transparency, font_name, font_color, font_scale)
        if img_bytes.getbuffer().nbytes > 0:
            plex_item.uploadPoster(filepath=img_bytes)
            return True
        return False
    except Exception as e:
        sys_logger.error(f"Failed to upload overlay to Plex: {e}")
        return False
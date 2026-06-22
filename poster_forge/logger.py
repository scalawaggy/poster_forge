import logging
import os
import colorlog
from logging.handlers import RotatingFileHandler
from config import load_settings, LOG_DIR

def setup_logger(name, log_file):
    settings = load_settings()
    # Pull verbosity level from Settings GUI
    level = logging.DEBUG if settings.get("DEBUG_MODE", False) else logging.INFO
    
    file_formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(name)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    
    # Caps files at 5MB and keeps 3 backups (e.g. webhook.log.1, webhook.log.2)
    handler = RotatingFileHandler(os.path.join(LOG_DIR, log_file), maxBytes=5*1024*1024, backupCount=3)
    handler.setFormatter(file_formatter)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Prevent duplicate prints during hot-reloading
    if logger.hasHandlers():
        logger.handlers.clear()
        
    logger.addHandler(handler)
    
    # Also print to the main Docker console with colors
    console = logging.StreamHandler()
    color_formatter = colorlog.ColoredFormatter(
        '%(log_color)s%(asctime)s | %(levelname)-8s | %(name)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        log_colors={
            'DEBUG':    'cyan',
            'INFO':     'green',
            'WARNING':  'yellow',
            'ERROR':    'red',
            'CRITICAL': 'red,bg_white',
        }
    )
    console.setFormatter(color_formatter)
    logger.addHandler(console)
    
    return logger

# Initialize our three Enterprise loggers
sys_logger = setup_logger('system', 'system.log')
wh_logger = setup_logger('webhook', 'webhook.log')
forge_logger = setup_logger('forge', 'forge.log')
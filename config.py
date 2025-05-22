import os
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

# Application settings
APP_NAME = "Novel Reader API"
DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "t")
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")

# Server settings
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# API settings
SHEET_ID = os.getenv("SHEET_ID")
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "en-US-ChristopherNeural")

# CORS settings
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")
CORS_METHODS = os.getenv("CORS_METHODS", "*").split(",")
CORS_HEADERS = os.getenv("CORS_HEADERS", "*").split(",")

# Logging settings
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

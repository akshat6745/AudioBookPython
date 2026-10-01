from pydantic_settings import BaseSettings
from typing import Optional
import os
import json

class Settings(BaseSettings):
    # App Config
    ENVIRONMENT: str = "production"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    HOST: str = "0.0.0.0"
    PORT: int = 8080

    # External Services
    SHEET_ID: Optional[str] = None
    
    # Supabase (kept for user auth + EPUB upload)
    SUPABASE_URL: str
    SUPABASE_KEY: str

    # Cloudflare D1 (novels + chapter metadata)
    CF_ACCOUNT_ID: Optional[str] = None
    CF_API_TOKEN: Optional[str] = None
    D1_DATABASE_ID: Optional[str] = None

    # Cloudflare R2 (chapter content files)
    R2_ACCESS_KEY_ID: Optional[str] = None
    R2_SECRET_ACCESS_KEY: Optional[str] = None
    R2_BUCKET_NAME: Optional[str] = None
    R2_ENDPOINT_URL: Optional[str] = None

    # Google Sign-In — the *Web* OAuth client ID, which is the audience the
    # Android app mints its ID tokens for. Google sign-in is disabled (503)
    # while this is unset.
    GOOGLE_CLIENT_ID: Optional[str] = None

    # Migration escape hatch: accept pre-migration plaintext passwords at
    # login. Defaults off so it fails closed; enable it only for the window
    # between deploying hash verification and running
    # scripts/hash_existing_passwords.py, then turn it back off.
    ALLOW_PLAINTEXT_LOGIN: bool = False

    # Access-token signing secret. No default on purpose — token issuing
    # fails loudly rather than signing with a guessable value. Generate with:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    # Rotating this value invalidates every outstanding token (global logout).
    JWT_SECRET: Optional[str] = None
    JWT_EXPIRY_DAYS: int = 30

    # Upload verification
    VERIFY_UPLOADS: bool = True # Automatically verify content after upload

    # TTS concurrency
    TTS_MAX_CONCURRENT: int = 10
    
    @property
    def is_local(self) -> bool:
        return self.ENVIRONMENT.lower() in ("local", "dev", "development")

    class Config:
        env_file = ".env"
        case_sensitive = True

def get_settings() -> Settings:
    """Get settings instance."""
    return settings

settings = Settings()

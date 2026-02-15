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
    
    # Supabase
    SUPABASE_URL: str
    SUPABASE_KEY: str
    
    # Upload verification
    VERIFY_UPLOADS: bool = True # Automatically verify content after upload
    
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

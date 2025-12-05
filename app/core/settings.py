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
    
    # Firebase
    FIREBASE_CREDENTIALS: Optional[str] = None
    
    @property
    def is_local(self) -> bool:
        return self.ENVIRONMENT.lower() in ("local", "dev", "development")

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()

from typing import Optional
from supabase import create_client, Client
from app.core.settings import settings
import structlog

logger = structlog.get_logger()

_supabase_client: Optional[Client] = None


def get_supabase_client() -> Client:
    """Get or create a Supabase client instance."""
    global _supabase_client
    
    if _supabase_client is None:
        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            raise RuntimeError("Supabase credentials not configured. Set SUPABASE_URL and SUPABASE_KEY.")
        
        logger.info("Initializing Supabase client", url=settings.SUPABASE_URL)
        _supabase_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
    
    return _supabase_client

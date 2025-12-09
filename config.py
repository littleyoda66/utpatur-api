# config.py
"""Configuration centralisée de l'application"""
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration de l'application"""
    
    # Neo4j / AuraDB
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str
    
    # OpenRouteService
    ors_api_key: str | None = None
    ors_base_url: str = "https://api.openrouteservice.org"
    
    # Overpass API
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    
    # Sécurité
    admin_token: str | None = None
    frontend_origin: str | None = None
    
    # Application
    app_name: str = "UtPaTur API"
    app_version: str = "2.0.0"
    debug: bool = False
    
    # Limites
    max_distance_km: float = 40.0
    max_segments: int = 5
    
    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    """Retourne une instance singleton des settings"""
    return Settings()
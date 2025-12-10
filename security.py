# security.py
"""Gestion de la sécurité et de l'authentification"""
import secrets
import logging
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
print(f"DEBUG - ADMIN_TOKEN loaded: {settings.admin_token}")

security = HTTPBearer()


async def require_admin(
    credentials: HTTPAuthorizationCredentials = Security(security)
) -> bool:
    """
    Dépendance FastAPI pour protéger les routes admin.
    
    Vérifie que le token Bearer correspond au ADMIN_TOKEN configuré.
    
    Usage:
        @router.get("/admin/something", dependencies=[Depends(require_admin)])
        async def admin_endpoint():
            ...
    
    Raises:
        HTTPException 401: Si le token est invalide
        HTTPException 500: Si ADMIN_TOKEN n'est pas configuré
    """
    if not settings.admin_token:
        logger.error("ADMIN_TOKEN non configuré dans l'environnement")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentification admin non configurée sur le serveur"
        )
    
    # Utiliser secrets.compare_digest pour éviter les timing attacks
    if not secrets.compare_digest(
        credentials.credentials,
        settings.admin_token
    ):
        logger.warning(f"Tentative d'accès admin avec token invalide")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token d'authentification invalide",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return True


def validate_distance(distance_km: float, max_allowed: float = 100.0) -> float:
    """Valide et normalise une distance"""
    if distance_km < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La distance ne peut pas être négative"
        )
    
    if distance_km > max_allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Distance maximale autorisée: {max_allowed} km"
        )
    
    return distance_km


def validate_segments(segments: int, max_allowed: int = 5) -> int:
    """Valide le nombre de segments"""
    if segments < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Le nombre de segments doit être >= 1"
        )
    
    if segments > max_allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Nombre maximum de segments: {max_allowed}"
        )
    
    return segments


def validate_coordinates(lat: float, lon: float) -> tuple[float, float]:
    """Valide des coordonnées GPS"""
    if not (-90 <= lat <= 90):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Latitude invalide (doit être entre -90 et 90)"
        )
    
    if not (-180 <= lon <= 180):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Longitude invalide (doit être entre -180 et 180)"
        )
    
    return lat, lon
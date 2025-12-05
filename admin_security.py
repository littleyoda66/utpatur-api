# admin_security.py
import os
from fastapi import Header, HTTPException, status

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")


async def require_admin(x_admin_token: str = Header(None)):
    """
    Protège les routes admin avec un simple token passé dans le header HTTP :
      X-Admin-Token: <valeur du token>

    ADMIN_TOKEN doit être défini dans les variables d'environnement du backend.
    """
    if ADMIN_TOKEN is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_TOKEN non configuré sur le serveur",
        )

    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Accès admin non autorisé",
        )

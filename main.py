# main.py
"""Application FastAPI principale"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import get_settings
from db import get_driver, close_driver, verify_connection, create_indexes
from models import HealthResponse
from routers import huts, admin, export


# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestion du cycle de vie de l'application"""
    # Startup
    logger.info("=" * 60)
    logger.info(f"Démarrage de {settings.app_name} v{settings.app_version}")
    logger.info("=" * 60)
    
    # Initialiser Neo4j
    try:
        driver = get_driver()
        if verify_connection():
            logger.info("✓ Connexion Neo4j vérifiée")
            create_indexes()
        else:
            logger.error("✗ Impossible de se connecter à Neo4j")
    except Exception as e:
        logger.error(f"✗ Erreur lors de l'initialisation Neo4j: {e}")
    
    logger.info("✓ Application prête")
    logger.info("=" * 60)
    
    yield
    
    # Shutdown
    logger.info("Arrêt de l'application...")
    close_driver()
    logger.info("✓ Application arrêtée proprement")


# Créer l'application FastAPI
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="""
    API pour la planification de raids à ski de randonnée nordique 
    en Laponie Suédoise et Norvégienne.
    
    ## Fonctionnalités
    
    * **Recherche de cabanes** - Trouve des cabanes par nom
    * **Cabanes atteignables** - Calcule les destinations possibles depuis une cabane
    * **Admin** - Gestion du graphe de cabanes (authentification requise)
    """,
    lifespan=lifespan,
)

# Configuration CORS
allowed_origins = []
if settings.frontend_origin:
    allowed_origins.append(settings.frontend_origin)
if settings.debug:
    allowed_origins.extend([
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ])

if not allowed_origins:
    logger.warning("⚠️  Aucune origine CORS configurée, utilisation de ['*']")
    allowed_origins = ["*"]

logger.info(f"CORS autorisé pour: {allowed_origins}")

# CORS ultra-permissif pour debug
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Temporaire pour tester
    allow_credentials=False,  # Désactivé temporairement
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes principales
@app.get(
    "/",
    tags=["root"],
    summary="Page d'accueil de l'API"
)
async def root():
    """Point d'entrée de l'API"""
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "status": "ok",
        "docs": "/docs",
        "health": "/health",
    }


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["root"],
    summary="Vérification de santé"
)
async def health():
    """Endpoint de health check pour Render et monitoring"""
    neo4j_ok = verify_connection()
    
    return HealthResponse(
        status="healthy" if neo4j_ok else "degraded",
        neo4j_connected=neo4j_ok,
        version=settings.app_version,
    )


# Gestion globale des erreurs
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Gestionnaire d'erreurs global"""
    logger.error(f"Erreur non gérée: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Erreur interne du serveur",
            "type": type(exc).__name__,
        }
    )


# Inclusion des routers
app.include_router(huts.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(export.router, prefix="/api/v1")

logger.info(f"✓ Routes configurées: {len(app.routes)} routes")
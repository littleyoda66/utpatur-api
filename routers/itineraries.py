# routers/itineraries.py
"""Router pour la sauvegarde et le chargement d'itinéraires avec code unique"""
import json
import logging
import secrets
import string
from datetime import datetime
from typing import List, Optional, Union
from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/itineraries", tags=["itineraries"])

# Caractères pour générer les codes (base32 sans ambiguïté: pas de 0/O, 1/I/L)
CODE_CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6


def generate_unique_code() -> str:
    """Génère un code aléatoire de 6 caractères"""
    return ''.join(secrets.choice(CODE_CHARS) for _ in range(CODE_LENGTH))


class SavedHut(BaseModel):
    """Cabane sauvegardée dans l'itinéraire"""
    hut_id: Union[str, int]
    name: str
    latitude: float
    longitude: float
    country_code: Optional[str] = None
    altitude: Optional[float] = None
    is_rest_day: bool = False
    
    @field_validator('hut_id', mode='before')
    @classmethod
    def convert_hut_id_to_str(cls, v):
        return str(v) if v is not None else v


class SavedSegment(BaseModel):
    """Segment sauvegardé entre deux cabanes"""
    distance_km: float
    elevation_gain: float
    elevation_loss: float
    geometry_polyline: Optional[str] = None  # Polyline encodée du tracé


class SavedStep(BaseModel):
    """Step (sous-segment) avec tracé géométrique"""
    from_hut_id: Optional[str] = None
    to_hut_id: Optional[str] = None
    distance_km: float
    dplus_m: float
    dminus_m: float
    geometry_polyline: Optional[str] = None


class SaveItineraryRequest(BaseModel):
    """Requête pour sauvegarder un itinéraire"""
    huts: List[SavedHut]
    segments: List[SavedSegment]
    steps: Optional[List[SavedStep]] = None  # Steps détaillés avec polylines
    start_date: str  # Format ISO: YYYY-MM-DD
    max_distance: Optional[float] = 35.0
    max_segments: Optional[int] = 2
    expedition_name: Optional[str] = None


class SaveItineraryResponse(BaseModel):
    """Réponse après sauvegarde d'un itinéraire"""
    code: str
    created_at: str
    huts_count: int
    total_distance: float


class LoadItineraryResponse(BaseModel):
    """Réponse lors du chargement d'un itinéraire"""
    code: str
    created_at: str
    start_date: str
    max_distance: float
    max_segments: int
    expedition_name: Optional[str]
    huts: List[SavedHut]
    segments: List[SavedSegment]
    steps: Optional[List[SavedStep]] = None


# Fonction pour obtenir la connexion Neo4j (à adapter selon ton setup)
def get_neo4j_driver():
    """Retourne le driver Neo4j - à adapter selon ton setup"""
    from neo4j import GraphDatabase
    import os
    
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")
    
    return GraphDatabase.driver(uri, auth=(user, password))


@router.post("", response_model=SaveItineraryResponse)
async def save_itinerary(request: SaveItineraryRequest):
    """
    Sauvegarde un itinéraire et retourne un code unique.
    Le code peut être utilisé pour recharger l'itinéraire plus tard.
    """
    try:
        driver = get_neo4j_driver()
        
        # Générer un code unique
        code = generate_unique_code()
        
        # Vérifier que le code n'existe pas déjà (boucle jusqu'à en trouver un unique)
        max_attempts = 10
        with driver.session() as session:
            for attempt in range(max_attempts):
                result = session.run(
                    "MATCH (i:Itinerary {code: $code}) RETURN i",
                    code=code
                )
                if result.single() is None:
                    break
                code = generate_unique_code()
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Impossible de générer un code unique"
                )
            
            # Calculer les totaux
            total_distance = sum(s.distance_km for s in request.segments)
            total_gain = sum(s.elevation_gain for s in request.segments)
            total_loss = sum(s.elevation_loss for s in request.segments)
            
            # Préparer les données pour Neo4j (JSON pour éviter les problèmes d'échappement)
            huts_data = [hut.model_dump() for hut in request.huts]
            segments_data = [seg.model_dump() for seg in request.segments]
            steps_data = [step.model_dump() for step in request.steps] if request.steps else []
            
            # Sauvegarder dans Neo4j
            created_at = datetime.utcnow().isoformat()
            
            session.run("""
                CREATE (i:Itinerary {
                    code: $code,
                    created_at: $created_at,
                    start_date: $start_date,
                    max_distance: $max_distance,
                    max_segments: $max_segments,
                    expedition_name: $expedition_name,
                    huts: $huts,
                    segments: $segments,
                    steps: $steps,
                    total_distance: $total_distance,
                    total_gain: $total_gain,
                    total_loss: $total_loss,
                    huts_count: $huts_count
                })
            """,
                code=code,
                created_at=created_at,
                start_date=request.start_date,
                max_distance=request.max_distance,
                max_segments=request.max_segments,
                expedition_name=request.expedition_name,
                huts=json.dumps(huts_data),
                segments=json.dumps(segments_data),
                steps=json.dumps(steps_data),
                total_distance=total_distance,
                total_gain=total_gain,
                total_loss=total_loss,
                huts_count=len(request.huts)
            )
        
        driver.close()
        
        logger.info(f"Itinéraire sauvegardé avec code: {code}")
        
        return SaveItineraryResponse(
            code=code,
            created_at=created_at,
            huts_count=len(request.huts),
            total_distance=total_distance
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erreur sauvegarde itinéraire: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erreur lors de la sauvegarde: {str(e)}"
        )


@router.get("/{code}", response_model=LoadItineraryResponse)
async def load_itinerary(code: str):
    """
    Charge un itinéraire à partir de son code unique.
    """
    try:
        # Normaliser le code (majuscules)
        code = code.upper().strip()
        
        if len(code) != CODE_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Le code doit contenir {CODE_LENGTH} caractères"
            )
        
        driver = get_neo4j_driver()
        
        with driver.session() as session:
            result = session.run(
                "MATCH (i:Itinerary {code: $code}) RETURN i",
                code=code
            )
            record = result.single()
            
            if record is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Itinéraire non trouvé"
                )
            
            itinerary = record["i"]
            
            # Parser les données JSON
            huts_data = json.loads(itinerary["huts"])
            segments_data = json.loads(itinerary["segments"])
            steps_data = json.loads(itinerary.get("steps", "[]") or "[]")
            
            huts = [SavedHut(**h) for h in huts_data]
            segments = [SavedSegment(**s) for s in segments_data]
            steps = [SavedStep(**s) for s in steps_data] if steps_data else None
        
        driver.close()
        
        logger.info(f"Itinéraire chargé: {code}")
        
        return LoadItineraryResponse(
            code=code,
            created_at=itinerary["created_at"],
            start_date=itinerary["start_date"],
            max_distance=itinerary["max_distance"],
            max_segments=itinerary["max_segments"],
            expedition_name=itinerary.get("expedition_name"),
            huts=huts,
            segments=segments,
            steps=steps
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erreur chargement itinéraire {code}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erreur lors du chargement: {str(e)}"
        )


@router.get("/{code}/exists")
async def check_itinerary_exists(code: str):
    """
    Vérifie si un itinéraire existe (sans charger toutes les données).
    """
    try:
        code = code.upper().strip()
        
        driver = get_neo4j_driver()
        
        with driver.session() as session:
            result = session.run(
                "MATCH (i:Itinerary {code: $code}) RETURN i.created_at as created_at, i.huts_count as huts_count",
                code=code
            )
            record = result.single()
        
        driver.close()
        
        if record is None:
            return {"exists": False}
        
        return {
            "exists": True,
            "created_at": record["created_at"],
            "huts_count": record["huts_count"]
        }
        
    except Exception as e:
        logger.error(f"Erreur vérification itinéraire {code}: {e}")
        return {"exists": False, "error": str(e)}

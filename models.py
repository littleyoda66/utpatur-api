# models.py
"""Modèles Pydantic pour la validation des données"""
from typing import Optional
from pydantic import BaseModel, Field, validator


class HutBase(BaseModel):
    """Modèle de base pour une cabane"""
    hut_id: int = Field(..., description="Identifiant unique de la cabane")
    name: str = Field(..., min_length=1, description="Nom de la cabane")
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    country_code: Optional[str] = Field(None, max_length=2)


class Hut(HutBase):
    """Cabane complète avec toutes les métadonnées"""
    osm_id: Optional[int] = None
    tourism: Optional[str] = None
    amenity: Optional[str] = None
    shelter_type: Optional[str] = None
    operator: Optional[str] = None
    
    class Config:
        json_schema_extra = {
            "example": {
                "hut_id": 123,
                "name": "Aktse Mountain Station",
                "latitude": 67.123,
                "longitude": 18.456,
                "country_code": "SE",
                "tourism": "alpine_hut",
            }
        }


class RouteStep(BaseModel):
    """Un segment d'itinéraire entre deux cabanes"""
    from_hut_id: int
    to_hut_id: int
    distance_km: float = Field(..., ge=0)
    dplus_m: float = Field(..., ge=0, description="Dénivelé positif en mètres")
    dminus_m: float = Field(..., ge=0, description="Dénivelé négatif en mètres")
    geometry_polyline: Optional[str] = None
    ors_skip: bool = Field(default=False, description="Segment calculé sans ORS")


class ReachableHut(HutBase):
    """Cabane atteignable avec infos d'itinéraire"""
    total_distance_km: float = Field(..., ge=0)
    total_dplus_m: float = Field(..., ge=0)
    total_dminus_m: float = Field(..., ge=0)
    segments: int = Field(..., ge=1)
    via: Optional[str] = Field(None, description="Cabane intermédiaire (si 2 segments)")
    steps: list[RouteStep]
    
    class Config:
        json_schema_extra = {
            "example": {
                "hut_id": 456,
                "name": "Sälka Hut",
                "latitude": 67.234,
                "longitude": 18.567,
                "country_code": "SE",
                "total_distance_km": 32.5,
                "total_dplus_m": 450.0,
                "total_dminus_m": 380.0,
                "segments": 2,
                "via": "Tjäktja Hut",
                "steps": []
            }
        }


class ReachableHutsResponse(BaseModel):
    """Réponse de l'endpoint /huts/{id}/reachable"""
    from_hut_id: int
    from_hut_name: str
    max_distance_km: float
    max_segments: int
    count: int
    huts: list[ReachableHut]


# --- Modèles Admin ---

class OverpassHutCandidate(BaseModel):
    """Candidat OSM trouvé via Overpass"""
    osm_id: int
    name: str
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    country_code: Optional[str] = None
    tourism: Optional[str] = None
    amenity: Optional[str] = None
    raw_tags: Optional[dict] = None


class ImportHutRequest(BaseModel):
    """Requête pour importer une cabane depuis Overpass"""
    name: str = Field(..., min_length=1)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    country_code: Optional[str] = Field(None, max_length=2)
    osm_id: Optional[int] = None
    raw_tags: Optional[dict] = None


class PreviewRouteRequest(BaseModel):
    """Requête pour prévisualiser un segment via ORS"""
    from_lat: float = Field(..., ge=-90, le=90)
    from_lon: float = Field(..., ge=-180, le=180)
    to_lat: float = Field(..., ge=-90, le=90)
    to_lon: float = Field(..., ge=-180, le=180)


class PreviewRouteResponse(BaseModel):
    """Réponse de prévisualisation ORS"""
    distance_km: float = Field(..., ge=0)
    dplus_m: float = Field(..., ge=0)
    dminus_m: float = Field(..., ge=0)
    geometry_polyline: Optional[str] = None


class CreateLinkRequest(BaseModel):
    """Requête pour créer un segment LINK"""
    from_hut_id: int
    to_hut_id: int
    distance_km: float = Field(..., ge=0, le=100)
    dplus_m: float = Field(..., ge=0)
    dminus_m: float = Field(..., ge=0)
    geometry_polyline: Optional[str] = None
    bidirectional: bool = Field(default=False)
    
    @validator('to_hut_id')
    def different_huts(cls, v, values):
        if 'from_hut_id' in values and v == values['from_hut_id']:
            raise ValueError('from_hut_id et to_hut_id doivent être différents')
        return v


class CreateLinkResponse(BaseModel):
    """Réponse de création de segment"""
    created_forward: bool
    created_backward: bool
    from_hut_id: int
    to_hut_id: int


class HealthResponse(BaseModel):
    """Réponse du endpoint /health"""
    status: str
    neo4j_connected: bool
    version: str
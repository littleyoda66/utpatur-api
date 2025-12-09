# routers/admin.py
"""Routes admin pour la gestion du graphe de cabanes"""
import logging
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
import requests

from db import run_query, run_write_query
from models import (
    Hut, OverpassHutCandidate, ImportHutRequest,
    PreviewRouteRequest, PreviewRouteResponse,
    CreateLinkRequest, CreateLinkResponse
)
from security import require_admin, validate_coordinates
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


# ===== RECHERCHE DE CABANES =====

@router.get(
    "/huts/search",
    response_model=list[Hut],
    summary="[Admin] Recherche fuzzy de cabanes existantes"
)
async def admin_search_huts(
    query: str = Query(..., min_length=2),
    limit: int = Query(20, ge=1, le=100)
):
    """Recherche de cabanes existantes dans Neo4j (pour l'admin)"""
    logger.info(f"[ADMIN] Recherche cabanes: '{query}'")
    
    cypher = """
    MATCH (h:Hut)
    WHERE toLower(h.name) CONTAINS toLower($query)
    RETURN
      h.hut_id       AS hut_id,
      h.name         AS name,
      h.latitude     AS latitude,
      h.longitude    AS longitude,
      h.country_code AS country_code,
      h.osm_id       AS osm_id,
      h.tourism      AS tourism,
      h.amenity      AS amenity,
      h.shelter_type AS shelter_type,
      h.operator     AS operator
    ORDER BY h.name
    LIMIT $limit
    """
    
    return run_query(cypher, {"query": query, "limit": limit})


@router.get(
    "/huts/overpass-search",
    response_model=list[OverpassHutCandidate],
    summary="[Admin] Recherche dans OSM via Overpass"
)
async def overpass_search(
    query: str = Query(..., min_length=2, description="Nom à chercher dans OSM"),
    limit: int = Query(20, ge=1, le=50)
):
    """
    Recherche de cabanes dans OpenStreetMap via Overpass API.
    
    Zone de recherche: Laponie (SE/NO) au nord du cercle polaire.
    """
    logger.info(f"[ADMIN] Recherche Overpass: '{query}'")
    
    # Bbox Laponie approximative
    SOUTH, WEST, NORTH, EAST = 66.0, 11.0, 71.5, 32.0
    
    # Requête Overpass simplifiée (1 seule passe)
    overpass_query = f"""
    [out:json][timeout:30];
    (
      node["tourism"~"alpine_hut|wilderness_hut|cabin"]["name"~"{query}", i]({SOUTH},{WEST},{NORTH},{EAST});
      way["tourism"~"alpine_hut|wilderness_hut|cabin"]["name"~"{query}", i]({SOUTH},{WEST},{NORTH},{EAST});
      node["amenity"="shelter"]["name"~"{query}", i]({SOUTH},{WEST},{NORTH},{EAST});
      way["amenity"="shelter"]["name"~"{query}", i]({SOUTH},{WEST},{NORTH},{EAST});
    );
    out center {limit};
    """
    
    try:
        resp = requests.post(
            settings.overpass_url,
            data={"data": overpass_query},
            timeout=35,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Erreur Overpass: {e}")
        # Ne pas bloquer l'admin, retourner []
        return []
    except ValueError:
        logger.error("Réponse Overpass invalide (JSON)")
        return []
    
    # Parser les résultats
    candidates = []
    seen_ids = set()
    
    for el in data.get("elements", []):
        osm_type = el.get("type")
        osm_id = el.get("id")
        
        if not osm_type or not osm_id:
            continue
        
        key = f"{osm_type}/{osm_id}"
        if key in seen_ids:
            continue
        seen_ids.add(key)
        
        # Coordonnées
        lat = el.get("lat")
        lon = el.get("lon")
        if lat is None or lon is None:
            center = el.get("center", {})
            lat = center.get("lat")
            lon = center.get("lon")
        
        if lat is None or lon is None:
            continue
        
        tags = el.get("tags", {}) or {}
        name = tags.get("name")
        if not name:
            continue
        
        candidates.append(OverpassHutCandidate(
            osm_id=osm_id,
            name=name,
            latitude=lat,
            longitude=lon,
            country_code=tags.get("addr:country"),
            tourism=tags.get("tourism"),
            amenity=tags.get("amenity"),
            raw_tags=tags,
        ))
        
        if len(candidates) >= limit:
            break
    
    logger.info(f"✓ {len(candidates)} candidats OSM trouvés")
    return candidates


# ===== IMPORT DE CABANES =====

@router.post(
    "/huts/import",
    response_model=Hut,
    status_code=status.HTTP_201_CREATED,
    summary="[Admin] Importer une cabane depuis OSM"
)
async def import_hut(body: ImportHutRequest):
    """Crée une nouvelle cabane dans Neo4j à partir d'OSM"""
    logger.info(f"[ADMIN] Import cabane: {body.name}")
    
    # Validation des coordonnées
    validate_coordinates(body.latitude, body.longitude)
    
    # Générer un nouveau hut_id
    max_id_row = run_write_query(
        "MATCH (h:Hut) RETURN coalesce(max(h.hut_id), 0) AS max_id"
    )
    max_id = max_id_row["max_id"] if max_id_row else 0
    new_id = int(max_id) + 1
    
    # Extraire quelques champs des tags OSM
    tags = body.raw_tags or {}
    tourism = tags.get("tourism")
    amenity = tags.get("amenity")
    shelter_type = tags.get("shelter_type")
    operator = tags.get("operator")
    tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
    
    # Créer le node Hut
    cypher = """
    CREATE (h:Hut {
        hut_id:       $hut_id,
        name:         $name,
        latitude:     $latitude,
        longitude:    $longitude,
        country_code: $country_code,
        osm_id:       $osm_id,
        tourism:      $tourism,
        amenity:      $amenity,
        shelter_type: $shelter_type,
        operator:     $operator,
        tags_json:    $tags_json
    })
    RETURN
      h.hut_id       AS hut_id,
      h.name         AS name,
      h.latitude     AS latitude,
      h.longitude    AS longitude,
      h.country_code AS country_code,
      h.osm_id       AS osm_id,
      h.tourism      AS tourism,
      h.amenity      AS amenity,
      h.shelter_type AS shelter_type,
      h.operator     AS operator
    """
    
    result = run_write_query(
        cypher,
        {
            "hut_id": new_id,
            "name": body.name,
            "latitude": body.latitude,
            "longitude": body.longitude,
            "country_code": body.country_code,
            "osm_id": body.osm_id,
            "tourism": tourism,
            "amenity": amenity,
            "shelter_type": shelter_type,
            "operator": operator,
            "tags_json": tags_json,
        }
    )
    
    if not result:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Échec de création de la cabane"
        )
    
    logger.info(f"✓ Cabane {new_id} créée: {body.name}")
    return result


# ===== PREVIEW ITINÉRAIRE VIA ORS =====

@router.post(
    "/routes/preview",
    response_model=PreviewRouteResponse,
    summary="[Admin] Prévisualiser un segment via ORS"
)
async def preview_route(body: PreviewRouteRequest):
    """
    Utilise OpenRouteService pour calculer distance, D+, D- et géométrie
    entre deux points.
    """
    logger.info(
        f"[ADMIN] Preview route: "
        f"({body.from_lat},{body.from_lon}) → ({body.to_lat},{body.to_lon})"
    )
    
    if not settings.ors_api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ORS_API_KEY non configuré"
        )
    
    # Validation
    validate_coordinates(body.from_lat, body.from_lon)
    validate_coordinates(body.to_lat, body.to_lon)
    
    url = f"{settings.ors_base_url}/v2/directions/foot-hiking"
    
    payload = {
        "coordinates": [
            [body.from_lon, body.from_lat],
            [body.to_lon, body.to_lat],
        ],
        "elevation": True,
        "geometry": True,
    }
    
    headers = {
        "Authorization": settings.ors_api_key,
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }
    
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Erreur ORS: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Erreur OpenRouteService: {str(e)}"
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Réponse ORS invalide (JSON)"
        )
    
    routes = data.get("routes", [])
    if not routes:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Aucune route trouvée par ORS"
        )
    
    route = routes[0]
    summary = route.get("summary", {})
    geometry = route.get("geometry")
    
    distance_m = summary.get("distance", 0)
    ascent = summary.get("ascent", 0)
    descent = summary.get("descent", 0)
    
    logger.info(
        f"✓ Route ORS calculée: {distance_m/1000:.1f}km, "
        f"D+{ascent:.0f}m, D-{descent:.0f}m"
    )
    
    return PreviewRouteResponse(
        distance_km=distance_m / 1000.0,
        dplus_m=ascent,
        dminus_m=descent,
        geometry_polyline=geometry,
    )


# ===== CRÉATION DE SEGMENTS =====

@router.post(
    "/links",
    response_model=CreateLinkResponse,
    status_code=status.HTTP_201_CREATED,
    summary="[Admin] Créer un segment LINK entre deux cabanes"
)
async def create_link(body: CreateLinkRequest):
    """
    Crée un segment LINK entre deux cabanes.
    
    Si bidirectional=True, crée aussi le segment inverse.
    """
    logger.info(
        f"[ADMIN] Création LINK: {body.from_hut_id} → {body.to_hut_id} "
        f"({body.distance_km}km, bidir={body.bidirectional})"
    )
    
    # Segment aller
    cypher_forward = """
    MATCH (a:Hut {hut_id: $from_id})
    MATCH (b:Hut {hut_id: $to_id})
    MERGE (a)-[r:LINK]->(b)
    SET r.distance_km = $distance_km,
        r.dplus_m = $dplus_m,
        r.dminus_m = $dminus_m,
        r.geometry_polyline = $geometry_polyline
    RETURN id(r) AS rel_id
    """
    
    result_forward = run_write_query(
        cypher_forward,
        {
            "from_id": body.from_hut_id,
            "to_id": body.to_hut_id,
            "distance_km": body.distance_km,
            "dplus_m": body.dplus_m,
            "dminus_m": body.dminus_m,
            "geometry_polyline": body.geometry_polyline,
        }
    )
    
    if not result_forward:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Une ou plusieurs cabanes introuvables"
        )
    
    created_forward = True
    created_backward = False
    
    # Segment retour si bidirectionnel
    if body.bidirectional:
        cypher_backward = """
        MATCH (a:Hut {hut_id: $from_id})
        MATCH (b:Hut {hut_id: $to_id})
        MERGE (b)-[r:LINK]->(a)
        SET r.distance_km = $distance_km,
            r.dplus_m = $dminus_m,
            r.dminus_m = $dplus_m,
            r.geometry_polyline = $geometry_polyline
        RETURN id(r) AS rel_id
        """
        
        result_backward = run_write_query(
            cypher_backward,
            {
                "from_id": body.from_hut_id,
                "to_id": body.to_hut_id,
                "distance_km": body.distance_km,
                "dplus_m": body.dplus_m,  # Inversé
                "dminus_m": body.dminus_m,  # Inversé
                "geometry_polyline": body.geometry_polyline,
            }
        )
        
        created_backward = result_backward is not None
    
    logger.info(
        f"✓ LINK créé (forward={created_forward}, backward={created_backward})"
    )
    
    return CreateLinkResponse(
        created_forward=created_forward,
        created_backward=created_backward,
        from_hut_id=body.from_hut_id,
        to_hut_id=body.to_hut_id,
    )
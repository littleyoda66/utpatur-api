# routers/huts.py
"""Routes publiques pour la gestion des cabanes"""
import logging
from fastapi import APIRouter, HTTPException, Query, Path, status
from db import run_query
from models import Hut, ReachableHut, ReachableHutsResponse
from security import validate_distance, validate_segments

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/huts",
    tags=["huts"],
)


@router.get(
    "/",
    response_model=list[Hut],
    summary="Liste toutes les cabanes",
    description="Retourne la liste complète des cabanes disponibles dans la base de données"
)
async def list_huts(
    limit: int = Query(1000, ge=1, le=5000, description="Nombre maximum de résultats"),
    offset: int = Query(0, ge=0, description="Offset pour la pagination")
):
    """Liste toutes les cabanes avec pagination"""
    logger.info(f"Liste des cabanes demandée (limit={limit}, offset={offset})")
    
    cypher = """
    MATCH (h:Hut)
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
    SKIP $offset
    LIMIT $limit
    """
    
    rows = run_query(cypher, {"limit": limit, "offset": offset})
    logger.info(f"✓ {len(rows)} cabanes retournées")
    
    return rows


@router.get(
    "/search",
    response_model=list[Hut],
    summary="Recherche de cabanes",
    description="Recherche des cabanes par nom (insensible à la casse)"
)
async def search_huts(
    query: str = Query(..., min_length=2, description="Texte à chercher dans le nom"),
    limit: int = Query(20, ge=1, le=100)
):
    """Recherche de cabanes par nom (autocomplete)"""
    logger.info(f"Recherche cabanes: '{query}' (limit={limit})")
    
    cypher = """
    MATCH (h:Hut)
    WHERE toLower(h.name) CONTAINS toLower($q)
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
    
    rows = run_query(cypher, {"q": query, "limit": limit})
    logger.info(f"✓ {len(rows)} résultats pour '{query}'")
    
    return rows

@router.get(
    "/trailheads",
    summary="Points d'accès en transports publics",
    description="Retourne les cabanes accessibles en transports publics avec les détails de connexion"
)
async def get_trailheads():
    """
    Récupère toutes les cabanes marquées comme Trailhead (points d'entrée/sortie)
    avec leurs informations de transport public.
    """
    logger.info("Liste des trailheads demandée")
    
    cypher = """
    MATCH (h:Hut:Trailhead)
    OPTIONAL MATCH (h)-[r:ACCESSIBLE_FROM]->(t:TransportHub)
    RETURN
      h.hut_id       AS hut_id,
      h.name         AS name,
      h.latitude     AS latitude,
      h.longitude    AS longitude,
      h.country_code AS country_code,
      CASE WHEN r IS NOT NULL THEN {
        mode: r.mode,
        line: r.line,
        duration: r.duration,
        hub: t.name,
        seasonal: COALESCE(r.seasonal, false)
      } ELSE null END AS transport
    ORDER BY h.name
    """
    
    rows = run_query(cypher, {})
    logger.info(f"✓ {len(rows)} trailheads retournés")
    
    return rows


@router.get(
    "/{hut_id}",
    response_model=Hut,
    summary="Détails d'une cabane",
    description="Retourne les informations détaillées d'une cabane"
)
async def get_hut(
    hut_id: int = Path(..., description="ID de la cabane")
):
    """Récupère les détails d'une cabane"""
    logger.info(f"Détails cabane demandés: {hut_id}")
    
    cypher = """
    MATCH (h:Hut {hut_id: $hut_id})
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
    
    rows = run_query(cypher, {"hut_id": hut_id})
    
    if not rows:
        logger.warning(f"Cabane {hut_id} non trouvée")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cabane {hut_id} non trouvée"
        )
    
    logger.info(f"✓ Cabane {hut_id} trouvée: {rows[0]['name']}")
    return rows[0]


@router.get(
    "/{hut_id}/reachable",
    response_model=ReachableHutsResponse,
    summary="Cabanes atteignables",
    description="""
    Calcule toutes les cabanes atteignables depuis une cabane donnée.
    
    Utilise l'algorithme de recherche de chemins dans le graphe Neo4j
    pour trouver toutes les destinations possibles en respectant les
    contraintes de distance et de nombre de segments.
    """
)
async def get_reachable_huts(
    hut_id: int = Path(..., description="ID de la cabane de départ"),
    max_distance_km: float = Query(
        35.0,
        ge=1.0,
        le=100.0,
        description="Distance totale maximale en km"
    ),
    max_segments: int = Query(
        2,
        ge=1,
        le=5,
        description="Nombre maximum de segments (1-5)"
    ),
):
    """
    Retourne toutes les cabanes atteignables depuis une cabane donnée.
    
    Pour chaque cabane destination, retourne le meilleur chemin (distance minimale).
    """
    # Validation
    validate_distance(max_distance_km)
    validate_segments(max_segments)
    
    logger.info(
        f"Recherche cabanes atteignables depuis {hut_id} "
        f"(max_dist={max_distance_km}km, max_seg={max_segments})"
    )
    
    # Vérifier que la cabane de départ existe
    check_cypher = """
    MATCH (h:Hut {hut_id: $hut_id})
    RETURN h.name AS name
    """
    check_rows = run_query(check_cypher, {"hut_id": hut_id})
    
    if not check_rows:
        logger.warning(f"Cabane de départ {hut_id} non trouvée")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cabane {hut_id} non trouvée"
        )
    
    from_hut_name = check_rows[0]["name"]
    
    # Recherche des cabanes atteignables
    # Note: On limite à 1000 chemins pour éviter les explosions
    cypher = """
    MATCH (start:Hut {hut_id: $start_id})
    MATCH p = (start)-[rels:LINK*1..5]->(dest:Hut)
    WHERE dest <> start
    WITH p, rels, dest,
         size(rels) AS segments,
         reduce(d = 0.0, r IN rels | d + coalesce(r.distance_km, 0.0)) AS total_distance_km,
         reduce(dp = 0.0, r IN rels | dp + coalesce(toFloat(r.dplus_m), 0.0)) AS total_dplus_m,
         reduce(dm = 0.0, r IN rels | dm + coalesce(toFloat(r.dminus_m), 0.0)) AS total_dminus_m
    WHERE segments <= $max_segments
      AND total_distance_km <= $max_distance_km
    WITH dest, p, rels, total_distance_km, total_dplus_m, total_dminus_m, segments,
         CASE WHEN segments = 2 THEN nodes(p)[1].name ELSE null END AS via
    ORDER BY total_distance_km ASC
    WITH dest,
         head(collect({
           total_distance_km: total_distance_km,
           total_dplus_m: total_dplus_m,
           total_dminus_m: total_dminus_m,
           segments: segments,
           via: via,
           rels: rels
         })) AS best
    RETURN
      dest.hut_id             AS hut_id,
      dest.name               AS name,
      dest.latitude           AS latitude,
      dest.longitude          AS longitude,
      dest.country_code       AS country_code,
      best.total_distance_km  AS total_distance_km,
      best.total_dplus_m      AS total_dplus_m,
      best.total_dminus_m     AS total_dminus_m,
      best.segments           AS segments,
      best.via                AS via,
      [r IN best.rels | {
        from_hut_id:        startNode(r).hut_id,
        to_hut_id:          endNode(r).hut_id,
        distance_km:        coalesce(r.distance_km, 0.0),
        dplus_m:            coalesce(toFloat(r.dplus_m), 0.0),
        dminus_m:           coalesce(toFloat(r.dminus_m), 0.0),
        geometry_polyline:  r.geometry_polyline,
        ors_skip:           coalesce(r.ors_skip, false)
      }] AS steps
    ORDER BY total_distance_km ASC, name ASC
    LIMIT 1000
    """
    
    rows = run_query(
        cypher,
        {
            "start_id": hut_id,
            "max_segments": max_segments,
            "max_distance_km": max_distance_km,
        },
    )
    
    logger.info(f"✓ {len(rows)} cabanes atteignables trouvées")
    
    return ReachableHutsResponse(
        from_hut_id=hut_id,
        from_hut_name=from_hut_name,
        max_distance_km=max_distance_km,
        max_segments=max_segments,
        count=len(rows),
        huts=rows,
    )
    
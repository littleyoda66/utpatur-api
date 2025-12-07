import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from neo4j import GraphDatabase, exceptions

# -------------------------------------------------------------------
# Connexion Neo4j / AuraDB
# -------------------------------------------------------------------

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD") or os.getenv("AURA_PASSWORD")

if not (NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD):
    raise RuntimeError(
        "NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD doivent être définies dans l'environnement."
    )

driver = GraphDatabase.driver(
    NEO4J_URI,
    auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
    max_connection_lifetime=3600,
)


def run_query(cypher: str, params: dict | None = None) -> list[dict]:
    """
    Exécute une requête Neo4j avec un retry automatique en cas de SessionExpired
    (connexion Aura fermée côté serveur) et renvoie une liste de dicts.
    """
    global driver
    params = params or {}

    for attempt in range(2):  # 1er essai + 1 retry
        try:
            with driver.session() as session:
                result = session.run(cypher, **params)
                # On consomme TOUT le résultat tant que la session est ouverte
                return [record.data() for record in result]

        except exceptions.SessionExpired:
            # On ferme le driver et on le recrée, puis on retente une fois
            driver.close()
            driver = GraphDatabase.driver(
                NEO4J_URI,
                auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
                max_connection_lifetime=3600,
            )
            if attempt == 1:
                # 2ᵉ échec → on propage l'erreur
                raise


# -------------------------------------------------------------------
# Modèles Pydantic
# -------------------------------------------------------------------

class Hut(BaseModel):
    hut_id: int
    name: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    country_code: Optional[str] = None


class RouteStep(BaseModel):
    from_hut_id: int
    to_hut_id: int
    distance_km: float
    dplus_m: float
    dminus_m: float
    geometry_polyline: Optional[str] = None
    ors_skip: bool = False


class ReachableHut(Hut):
    total_distance_km: float
    total_dplus_m: float
    total_dminus_m: float
    segments: int
    via: Optional[str] = None
    steps: List[RouteStep]



# -------------------------------------------------------------------
# Router
# -------------------------------------------------------------------

router = APIRouter(
    prefix="/huts",
    tags=["huts"],
)


# -------------------------------------------------------------------
# 1. Liste de toutes les cabanes
# -------------------------------------------------------------------

@router.get("/", response_model=List[Hut])
async def list_huts():
    """
    Renvoie la liste de toutes les cabanes connues dans AuraDB,
    avec les infos de base nécessaires pour sélectionner la cabane de départ.
    """
    rows = run_query(
        """
        MATCH (h:Hut)
        RETURN
          h.hut_id       AS hut_id,
          h.name         AS name,
          h.latitude     AS latitude,
          h.longitude    AS longitude,
          h.country_code AS country_code
        ORDER BY h.name
        """
    )
    # rows est déjà une liste de dicts compatible avec le modèle Hut
    return rows


# -------------------------------------------------------------------
# 2. Détail d'une cabane
# -------------------------------------------------------------------

@router.get("/{hut_id}", response_model=Hut)
async def get_hut(hut_id: int):
    """
    Renvoie les informations de base pour une cabane donnée.
    """
    rows = run_query(
        """
        MATCH (h:Hut {hut_id: $hut_id})
        RETURN
          h.hut_id       AS hut_id,
          h.name         AS name,
          h.latitude     AS latitude,
          h.longitude    AS longitude,
          h.country_code AS country_code
        """,
        {"hut_id": hut_id},
    )

    record = rows[0] if rows else None

    if record is None:
        raise HTTPException(status_code=404, detail=f"Hut {hut_id} non trouvée")

    return record


# -------------------------------------------------------------------
# 3. Cabanes atteignables depuis une cabane source
# -------------------------------------------------------------------

@router.get(
    "/{hut_id}/reachable",
    response_model=List[ReachableHut],
)
async def get_reachable_huts(
    hut_id: int,
    max_distance_km: float = Query(
        35.0,
        description="Distance totale maximale (en km) pour atteindre la cabane d'arrivée",
    ),
    max_segments: int = Query(
        2,
        ge=1,
        le=5,
        description="Nombre maximal de segments (LINK) dans le chemin",
    ),
):
    """
    Renvoie les cabanes atteignables depuis une cabane donnée, en 1 à max_segments
    relations LINK, pour une distance totale <= max_distance_km.

    Pour chaque cabane d'arrivée, on renvoie la *meilleure* combinaison (distance minimale).
    """
    if max_distance_km <= 0:
        return []

    # Vérifier que la cabane de départ existe
    exists_rows = run_query(
        "MATCH (h:Hut {hut_id: $hut_id}) RETURN h LIMIT 1",
        {"hut_id": hut_id},
    )
    if not exists_rows:
        raise HTTPException(
            status_code=404,
            detail=f"Hut {hut_id} non trouvée",
        )

    cypher = """
    MATCH (start:Hut {hut_id: $start_id})

    // on cherche tous les chemins de 1 à 5 segments
    MATCH p = (start)-[rels:LINK*1..5]->(dest:Hut)
    WHERE dest <> start

    WITH dest, p, rels,
         size(rels) AS segments,
         reduce(d  = 0.0, r IN rels | d  + coalesce(r.distance_km, 0.0))       AS total_distance_km,
         reduce(dp = 0.0, r IN rels | dp + coalesce(toFloat(r.dplus_m),  0.0)) AS total_dplus_m,
         reduce(dm = 0.0, r IN rels | dm + coalesce(toFloat(r.dminus_m), 0.0)) AS total_dminus_m

    // ici on applique les paramètres max_segments et max_distance_km
    WHERE segments <= $max_segments
      AND total_distance_km <= $max_distance_km

    // on calcule le "via" si exactement 2 segments
    WITH dest,
         p,
         rels,
         total_distance_km,
         total_dplus_m,
         total_dminus_m,
         segments,
         CASE
           WHEN segments = 2 THEN nodes(p)[1].name
           ELSE null
         END AS via
    ORDER BY total_distance_km ASC, segments ASC

    // on garde, pour chaque cabane d’arrivée, le meilleur chemin (distance mini)
    WITH dest,
         head(collect({
           total_distance_km: total_distance_km,
           total_dplus_m:     total_dplus_m,
           total_dminus_m:    total_dminus_m,
           segments:          segments,
           via:               via,
           rels:              rels
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
    """
    rows = run_query(
        cypher,
        {
            "start_id": hut_id,
            "max_segments": max_segments,
            "max_distance_km": max_distance_km,
        },
    )

    # rows est une liste de dicts compatible avec ReachableHut
    return rows

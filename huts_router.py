import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from neo4j import GraphDatabase


# -------------------------------------------------------------------
# Connexion Neo4j / AuraDB
# -------------------------------------------------------------------

NEO4J_URI = os.getenv("NEO4J_URI") or os.getenv("AURA_URI")
NEO4J_USER = os.getenv("NEO4J_USER") or os.getenv("AURA_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD") or os.getenv("AURA_PASSWORD")

if not (NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD):
    raise RuntimeError(
        "NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD (ou AURA_*) doivent être définis dans les variables d'environnement."
    )

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


# -------------------------------------------------------------------
# Modèles Pydantic
# -------------------------------------------------------------------

class Hut(BaseModel):
    hut_id: int
    name: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    country_code: Optional[str] = None


from typing import List, Optional
# ...

class ReachableHut(Hut):
    total_distance_km: float
    total_dplus_m: float
    total_dminus_m: float
    segments: int
    via: Optional[str] = None


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
    with driver.session() as session:
        result = session.run(
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
        rows = [record.data() for record in result]

    return rows


# -------------------------------------------------------------------
# 2. Détail d'une cabane (optionnel mais pratique)
# -------------------------------------------------------------------

@router.get("/{hut_id}", response_model=Hut)
async def get_hut(hut_id: int):
    """
    Renvoie les informations de base pour une cabane donnée.
    """
    with driver.session() as session:
        result = session.run(
            """
            MATCH (h:Hut {hut_id: $hut_id})
            RETURN
              h.hut_id       AS hut_id,
              h.name         AS name,
              h.latitude     AS latitude,
              h.longitude    AS longitude,
              h.country_code AS country_code
            """,
            hut_id=hut_id,
        )
        record = result.single()

    if not record:
        raise HTTPException(status_code=404, detail=f"Hut {hut_id} non trouvée")

    return record.data()


# -------------------------------------------------------------------
# 3. Cabanes atteignables en 1–2 segments depuis une cabane source
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

    Pour chaque cabane d'arrivée, on renvoie la *meilleure* combinaison (distance minimale),
    avec :

      - total_distance_km : distance cumulée de tous les segments
      - total_dplus_m     : D+ cumulé
      - total_dminus_m    : D- cumulé
      - segments          : nombre de segments dans le chemin (1 ou 2 typiquement)
    """
    if max_distance_km <= 0:
        return []

    with driver.session() as session:
        result = session.run(
            "MATCH (h:Hut {hut_id: $hut_id}) RETURN h LIMIT 1",
            hut_id=hut_id,
        )
        if not result.single():
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
               via:               via
             })) AS best

        RETURN
          dest.hut_id             AS hut_id,
          dest.name               AS name,
          dest.latitude           AS latitude,
          dest.longitude          AS longitude,
          best.total_distance_km  AS total_distance_km,
          best.total_dplus_m      AS total_dplus_m,
          best.total_dminus_m     AS total_dminus_m,
          best.segments           AS segments,
          best.via                AS via
        ORDER BY total_distance_km ASC, name ASC
        """



        result = session.run(
            cypher,
            start_id=hut_id,
            max_segments=max_segments,
            max_distance_km=max_distance_km,
        )

        rows = [record.data() for record in result]

    return rows

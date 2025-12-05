import os
from fastapi import FastAPI, Query, Path
from fastapi.middleware.cors import CORSMiddleware
from neo4j import GraphDatabase
from huts_router import router as huts_router
from admin_router import router as admin_router

# ------------------------------------------------------------------
# Configuration Neo4j (AuraDB) via variables d'environnement
# ------------------------------------------------------------------

NEO4J_URI = os.environ.get("NEO4J_URI")
NEO4J_USER = os.environ.get("NEO4J_USER")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD")

if not all([NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD]):
    raise RuntimeError("NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD doivent être définies dans l'environnement.")

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# ------------------------------------------------------------------
# FastAPI app
# ------------------------------------------------------------------

app = FastAPI(title="utpatur-api")

app.include_router(huts_router)
app.include_router(admin_router)

# CORS : autoriser le frontend Render (et éventuellement localhost)
frontend_origin = os.environ.get("FRONTEND_ORIGIN", "")
allowed_origins = [o for o in [frontend_origin, "http://localhost:5173", "http://localhost:3000"] if o]

if not allowed_origins:
    allowed_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# Hooks de lifecycle
# ------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    # Vérifier la connexion Neo4j au démarrage
    with driver.session() as session:
        session.run("RETURN 1")


@app.on_event("shutdown")
def on_shutdown():
    driver.close()

# ------------------------------------------------------------------
# Endpoints existants (exemples) – garde les tiens ici
# ------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok", "service": "utpatur-api"}


@app.get("/health")
def health():
    return {"status": "ok"}

# ------------------------------------------------------------------
# 1) /huts/search – recherche de cabanes
# ------------------------------------------------------------------

@app.get("/huts/search")
async def search_huts(
    query: str = Query(..., min_length=2, description="Texte à chercher dans le nom de la cabane"),
    limit: int = Query(20, ge=1, le=100),
):
    """
    Recherche de cabanes par nom (autocomplete).
    """
    cypher = """
    MATCH (h:Hut)
    WHERE toLower(h.name) CONTAINS toLower($q)
    RETURN
      h.hut_id       AS hut_id,
      h.name         AS name,
      h.latitude     AS latitude,
      h.longitude    AS longitude,
      h.country_code AS country_code
    ORDER BY name
    LIMIT $limit
    """

    with driver.session() as session:
        result = session.run(cypher, q=query, limit=limit)
        rows = [record.data() for record in result]

    return rows

# ------------------------------------------------------------------
# 2) /huts/{hut_id}/reachable – cabanes accessibles 1–2 segments
# ------------------------------------------------------------------

@app.get("/huts/{hut_id}/reachable")
async def reachable_huts(
    hut_id: int = Path(..., description="hut_id de la cabane de départ"),
    max_distance_km: float = Query(35.0, ge=0.0),
    max_segments: int = Query(2, ge=1, le=2),
):
    """
    Cabanes accessibles depuis une cabane donnée, en 1 ou 2 segments,
    avec une distance totale <= max_distance_km.
    """
    cypher = """
    MATCH (start:Hut {hut_id: $hutId})
    MATCH p = (start)-[rels:LINK*1..$maxSegments]->(dest:Hut)
    WHERE dest <> start
    WITH start, dest, rels, p,
         reduce(dist = 0.0,  r IN rels | dist  + coalesce(r.distance_km, 0.0)) AS distance_km,
         reduce(dplus = 0.0,  r IN rels | dplus  + coalesce(r.dplus_m,   0.0)) AS dplus_m,
         reduce(dminus = 0.0, r IN rels | dminus + coalesce(r.dminus_m,  0.0)) AS dminus_m,
         size(rels) AS segments
    WHERE distance_km <= $maxDistanceKm

    // on garde la route la plus courte par (dest, segments)
    WITH start, dest, segments,
         distance_km, dplus_m, dminus_m, p
    ORDER BY distance_km
    WITH start, dest, segments, collect({
      distance_km: distance_km,
      dplus_m: dplus_m,
      dminus_m: dminus_m,
      p: p
    })[0] AS best
    WITH start, dest, segments,
         best.distance_km AS distance_km,
         best.dplus_m     AS dplus_m,
         best.dminus_m    AS dminus_m,
         best.p           AS p,
         CASE
           WHEN segments = 2 THEN nodes(p)[1].name
           ELSE null
         END AS via
    RETURN
      dest.hut_id AS hut_id,
      dest.name   AS name,
      distance_km,
      dplus_m,
      dminus_m,
      segments,
      via
    ORDER BY segments ASC, distance_km ASC
    """

    with driver.session() as session:
        result = session.run(
            cypher,
            hutId=hut_id,
            maxDistanceKm=max_distance_km,
            maxSegments=max_segments,
        )
        rows = [record.data() for record in result]

    return {
        "from_hut_id": hut_id,
        "max_distance_km": max_distance_km,
        "max_segments": max_segments,
        "results": rows,
    }

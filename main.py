from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from neo4j import GraphDatabase
from pydantic import BaseModel
from typing import List, Optional
import os
from dotenv import load_dotenv

# Charger les variables d'environnement depuis .env en local
load_dotenv()

NEO4J_URI = os.environ["NEO4J_URI"]
NEO4J_USER = os.environ["NEO4J_USERNAME"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

app = FastAPI(title="UtPaTur API")

# --- CORS : on ouvrira plus finement plus tard ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # plus tard: ["https://utpatur.app", "https://www.utpatur.app"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Hut(BaseModel):
    hut_id: Optional[int] = None
    name: Optional[str] = None
    latitude: float
    longitude: float
    country_code: Optional[str] = None
    amenity: Optional[str] = None
    tourism: Optional[str] = None
    shelter_type: Optional[str] = None
    operator: Optional[str] = None
    osm_id: Optional[int] = None
    routes_codes: Optional[List[str]] = None
    routes_labels: Optional[List[str]] = None


@app.on_event("shutdown")
def close_driver():
    driver.close()


@app.get("/health")
def health():
    with driver.session() as session:
        result = session.run("MATCH (h:Hut) RETURN count(h) AS huts LIMIT 1")
        record = result.single()
        return {
            "status": "ok",
            "huts_count_sample": record["huts"] if record else 0,
        }


@app.get("/huts", response_model=List[Hut])
def list_huts(limit: int = 200):
    """
    Retourne une liste de huts (jusqu'à 'limit').
    """
    with driver.session() as session:
        result = session.run(
            """
            MATCH (h:Hut)
            RETURN h
            LIMIT $limit
            """,
            limit=limit,
        )

        huts: list[dict] = []
        for record in result:
            node = record["h"]
            huts.append(
                {
                    "hut_id": node.get("hut_id"),
                    "name": node.get("name"),
                    "latitude": node.get("latitude"),
                    "longitude": node.get("longitude"),
                    "country_code": node.get("country_code"),
                    "amenity": node.get("amenity"),
                    "tourism": node.get("tourism"),
                    "shelter_type": node.get("shelter_type"),
                    "operator": node.get("operator"),
                    "osm_id": node.get("osm_id"),
                    "routes_codes": node.get("routes_codes"),
                    "routes_labels": node.get("routes_labels"),
                }
            )

    return huts

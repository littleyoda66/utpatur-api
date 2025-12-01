from fastapi import FastAPI
from neo4j import GraphDatabase
import os
from dotenv import load_dotenv

load_dotenv()

NEO4J_URI = os.environ["NEO4J_URI"]
NEO4J_USER = os.environ["NEO4J_USERNAME"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

app = FastAPI(title="UtPaTur API")

@app.on_event("shutdown")
def close_driver():
    driver.close()

@app.get("/health")
def health():
    # Petite requête test pour vérifier la connexion AuraDB
    with driver.session() as session:
        result = session.run("MATCH (h:Hut) RETURN count(h) AS huts LIMIT 1")
        record = result.single()
        return {
            "status": "ok",
            "huts_count_sample": record["huts"] if record else 0,
        }

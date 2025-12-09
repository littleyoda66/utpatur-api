# db.py
"""Gestion de la connexion Neo4j avec retry automatique"""
import time
import logging
from typing import Any
from neo4j import GraphDatabase, Driver
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Instance globale du driver
_driver: Driver | None = None


def get_driver() -> Driver:
    """Retourne le driver Neo4j (singleton)"""
    global _driver
    
    if _driver is None:
        logger.info("Création de la connexion Neo4j...")
        _driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
            max_connection_lifetime=3600,
            max_connection_pool_size=50,
            connection_acquisition_timeout=60,
        )
        logger.info("✓ Connexion Neo4j établie")
    
    return _driver


def close_driver():
    """Ferme proprement le driver Neo4j"""
    global _driver
    if _driver:
        logger.info("Fermeture de la connexion Neo4j...")
        _driver.close()
        _driver = None


def run_query(
    cypher: str,
    params: dict[str, Any] | None = None,
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    """
    Exécute une requête Cypher avec retry automatique.
    
    Args:
        cypher: Requête Cypher à exécuter
        params: Paramètres de la requête (toujours utiliser des paramètres !)
        max_retries: Nombre maximum de tentatives
        
    Returns:
        Liste de dictionnaires avec les résultats
        
    Raises:
        ServiceUnavailable: Si Neo4j est injoignable après tous les retries
        SessionExpired: Si la session expire après tous les retries
    """
    params = params or {}
    driver = get_driver()
    
    for attempt in range(max_retries):
        try:
            with driver.session() as session:
                result = session.run(cypher, **params)
                # Consommer immédiatement les résultats
                return [record.data() for record in result]
                
        except (SessionExpired, ServiceUnavailable, TransientError) as e:
            if attempt == max_retries - 1:
                logger.error(f"Échec après {max_retries} tentatives: {e}")
                raise
            
            wait_time = 2 ** attempt  # Exponential backoff
            logger.warning(
                f"Erreur Neo4j (tentative {attempt + 1}/{max_retries}): {e}. "
                f"Nouvelle tentative dans {wait_time}s..."
            )
            time.sleep(wait_time)
    
    return []  # Jamais atteint, mais pour le type checker


def run_write_query(
    cypher: str,
    params: dict[str, Any] | None = None,
    max_retries: int = 3,
) -> dict[str, Any] | None:
    """
    Exécute une requête d'écriture et retourne un seul résultat.
    
    Utile pour les CREATE/MERGE qui retournent un seul enregistrement.
    """
    params = params or {}
    driver = get_driver()
    
    for attempt in range(max_retries):
        try:
            with driver.session() as session:
                result = session.run(cypher, **params)
                record = result.single()
                return record.data() if record else None
                
        except (SessionExpired, ServiceUnavailable, TransientError) as e:
            if attempt == max_retries - 1:
                logger.error(f"Échec après {max_retries} tentatives: {e}")
                raise
            
            wait_time = 2 ** attempt
            logger.warning(
                f"Erreur Neo4j (tentative {attempt + 1}/{max_retries}): {e}. "
                f"Nouvelle tentative dans {wait_time}s..."
            )
            time.sleep(wait_time)
    
    return None


def verify_connection() -> bool:
    """Vérifie que la connexion Neo4j fonctionne"""
    try:
        driver = get_driver()
        with driver.session() as session:
            result = session.run("RETURN 1 AS test")
            record = result.single()
            return record and record["test"] == 1
    except Exception as e:
        logger.error(f"Erreur de connexion Neo4j: {e}")
        return False


def create_indexes():
    """Crée les indices Neo4j nécessaires"""
    indexes = [
        "CREATE INDEX hut_id_index IF NOT EXISTS FOR (h:Hut) ON (h.hut_id)",
        "CREATE INDEX hut_name_index IF NOT EXISTS FOR (h:Hut) ON (h.name)",
        "CREATE INDEX hut_location_index IF NOT EXISTS FOR (h:Hut) ON (h.latitude, h.longitude)",
    ]
    
    driver = get_driver()
    with driver.session() as session:
        for idx_query in indexes:
            try:
                session.run(idx_query)
                logger.info(f"✓ Index créé: {idx_query[:50]}...")
            except Exception as e:
                logger.warning(f"Impossible de créer l'index: {e}")
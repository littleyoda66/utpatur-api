import os
import time
import math
import requests
from neo4j import GraphDatabase
from dotenv import load_dotenv

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

load_dotenv()

NEO4J_URI = os.environ["NEO4J_URI"]
NEO4J_USERNAME = os.environ["NEO4J_USERNAME"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]

ORS_API_KEY = os.environ.get("ORS_API_KEY")
if not ORS_API_KEY:
    raise RuntimeError(
        "Variable d'environnement ORS_API_KEY non définie. "
        "Définis-la (par ex. dans .env) avant de lancer le script."
    )

# Profil rando - par défaut, format JSON avec polyligne encodée
# (data["routes"][0]["geometry"])
ORS_URL = "https://api.openrouteservice.org/v2/directions/foot-hiking"

# Batchs pour éviter d'exploser le quota ORS
BATCH_SIZE = 20
SLEEP_BETWEEN_CALLS = 2.0  # secondes


# -------------------------------------------------------------------
# Connexion Neo4j
# -------------------------------------------------------------------
def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))


# -------------------------------------------------------------------
# Récupérer les liens à enrichir (IDs + coords)
# -------------------------------------------------------------------
def fetch_links_to_enrich(session, limit=BATCH_SIZE):
    """
    Récupère des liens (a:Hut)-[l:LINK]->(b:Hut) à enrichir avec ORS.

    On cible les liens pour lesquels :
      - la géométrie n'est pas encore définie (l.geometry_polyline IS NULL)
      - ET qui ne sont pas marqués comme ors_skip = true
      - ET dont les huts ont des coordonnées valides.
    """
    query = """
    MATCH (a:Hut)-[l:LINK]->(b:Hut)
    WHERE l.geometry_polyline IS NULL
      AND coalesce(l.ors_skip, false) = false
      AND a.latitude IS NOT NULL AND a.longitude IS NOT NULL
      AND b.latitude IS NOT NULL AND b.longitude IS NOT NULL
    RETURN
      a.hut_id    AS from_id,
      b.hut_id    AS to_id,
      a.name      AS from_name,
      b.name      AS to_name,
      a.longitude AS from_lon,
      a.latitude  AS from_lat,
      b.longitude AS to_lon,
      b.latitude  AS to_lat
    LIMIT $limit
    """
    result = session.run(query, limit=limit)
    return list(result)


# -------------------------------------------------------------------
# Marquer un lien comme à ignorer (échec ORS définitif)
# -------------------------------------------------------------------
def mark_link_as_failed(session, from_id, to_id, reason: str):
    """
    Marque le lien (a)-[l:LINK]->(b) comme à ignorer pour les prochains runs.
    On stocke aussi un petit message explicatif dans l.ors_reason.
    """
    query = """
    MATCH (a:Hut {hut_id: $from_id})-[l:LINK]->(b:Hut {hut_id: $to_id})
    SET l.ors_skip   = true,
        l.ors_reason = $reason
    RETURN count(l) AS updated_count
    """
    result = session.run(
        query,
        from_id=from_id,
        to_id=to_id,
        reason=reason[:500],
    )
    record = result.single()
    return record["updated_count"] if record and "updated_count" in record else 0


# -------------------------------------------------------------------
# Supprimer les liens self (même hut_id des deux côtés)
# -------------------------------------------------------------------
def delete_self_link(session, hut_id: int):
    """
    Supprime tous les liens (a:Hut {hut_id})-[l:LINK]->(b:Hut {hut_id})
    où le hut_id est identique des deux côtés, même si a et b sont
    deux nodes Neo4j différents.
    """
    query = """
    MATCH (a:Hut {hut_id: $hut_id})-[l:LINK]->(b:Hut {hut_id: $hut_id})
    DELETE l
    RETURN count(l) AS deleted_count
    """
    result = session.run(query, hut_id=hut_id)
    record = result.single()
    return record["deleted_count"] if record and "deleted_count" in record else 0


# -------------------------------------------------------------------
# Appel ORS pour une paire de huts
# -------------------------------------------------------------------
def call_ors(from_lon, from_lat, to_lon, to_lat, from_name="?", to_name="?"):
    """
    Appelle ORS entre deux points [lon, lat].

    Retourne (distance_km, dplus_m, dminus_m, geometry_polyline)
    ou None en cas d'erreur liée à ce lien.

    En cas de dépassement de quota (429), on lève une RuntimeError
    pour arrêter proprement le script.
    """
    body = {
        "coordinates": [
            [from_lon, from_lat],  # ORS = [lon, lat]
            [to_lon, to_lat],
        ],
        "elevation": True,  # pour récupérer ascent / descent dans le summary
    }

    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(ORS_URL, headers=headers, json=body, timeout=30)
    except Exception as e:
        print(f"  ERREUR réseau ORS pour {from_name} -> {to_name}: {e}")
        return None

    if resp.status_code == 429:
        # Rate limit global : on arrête le script proprement
        print(f"  ERREUR ORS 429 (Rate Limit Exceeded) pour {from_name} -> {to_name}")
        raise RuntimeError("ORS rate limit exceeded (HTTP 429)")

    if resp.status_code != 200:
        print(f"  ERREUR ORS {resp.status_code} pour {from_name} -> {to_name}")
        try:
            print("   ", resp.text[:300], "...")
        except Exception:
            pass
        return None

    try:
        data = resp.json()
    except Exception as e:
        print(f"  ERREUR JSON ORS pour {from_name} -> {to_name}: {e}")
        try:
            print("   Réponse brute:", resp.text[:300], "...")
        except Exception:
            pass
        return None

    distance_m = None
    ascent = 0.0
    descent = 0.0
    geometry_polyline = None

    # Format JSON "classique" : data["routes"][0]
    if isinstance(data, dict) and "routes" in data:
        try:
            route = data["routes"][0]
            summary = route.get("summary", {})
            distance_m = float(summary["distance"])
            ascent = float(summary.get("ascent", 0.0))
            descent = float(summary.get("descent", 0.0))
            geometry_polyline = route.get("geometry")
        except (KeyError, IndexError, TypeError, ValueError) as e:
            print(f"  ERREUR parsing 'routes' pour {from_name} -> {to_name}: {e}")
            return None

    # Ancien format GeoJSON (features) - fallback
    elif isinstance(data, dict) and "features" in data:
        try:
            feature = data["features"][0]
            props = feature.get("properties", {})
            summary = props.get("summary", {})
            distance_m = float(summary.get("distance", props.get("distance")))
            ascent = float(summary.get("ascent", props.get("ascent", 0.0)))
            descent = float(summary.get("descent", props.get("descent", 0.0)))
            # On n'a pas de polyline encodée ici, juste une géométrie GeoJSON.
            # On pourrait la convertir, mais pour l'instant on stocke une string vide.
            geometry_polyline = ""
        except (KeyError, IndexError, TypeError, ValueError) as e:
            print(f"  ERREUR parsing 'features' pour {from_name} -> {to_name}: {e}")
            return None

    else:
        print(f"  Réponse ORS inattendue pour {from_name} -> {to_name}: ni 'routes' ni 'features'")
        return None

    if distance_m is None:
        print(f"  ERREUR: distance manquante dans la réponse ORS pour {from_name} -> {to_name}")
        return None

    if geometry_polyline is None:
        # Cas anormal en JSON moderne : on préfère marquer en échec
        print(f"  ERREUR: pas de geometry_polyline pour {from_name} -> {to_name}")
        return None

    distance_km = distance_m / 1_000.0
    return distance_km, ascent, descent, geometry_polyline


# -------------------------------------------------------------------
# Mise à jour d'un lien en base, via hut_id
# -------------------------------------------------------------------
def update_link_in_neo4j(session, from_id, to_id, distance_km, dplus_m, dminus_m, geometry_polyline):
    """
    Met à jour le lien (a)-[l:LINK]->(b) identifié par les hut_id.

    On met à jour distance_km / dplus_m / dminus_m ET geometry_polyline.
    """
    query = """
    MATCH (a:Hut {hut_id: $from_id}),
          (b:Hut {hut_id: $to_id})
    MATCH (a)-[l:LINK]->(b)
    SET l.distance_km       = $distance_km,
        l.dplus_m           = $dplus_m,
        l.dminus_m          = $dminus_m,
        l.geometry_polyline = $geometry_polyline
    RETURN count(l) AS updated_count
    """
    result = session.run(
        query,
        from_id=from_id,
        to_id=to_id,
        distance_km=float(distance_km),
        dplus_m=float(dplus_m),
        dminus_m=float(dminus_m),
        geometry_polyline=geometry_polyline,
    )
    record = result.single()
    return record["updated_count"] if record and "updated_count" in record else 0


# -------------------------------------------------------------------
# Programme principal
# -------------------------------------------------------------------
def main():
    driver = get_driver()
    try:
        with driver.session() as session:
            while True:
                links = fetch_links_to_enrich(session, limit=BATCH_SIZE)
                if not links:
                    print("Plus aucun lien à enrichir, terminé.")
                    break

                print(f"Traitement d’un batch de {len(links)} liens…")

                for rec in links:
                    from_id = rec["from_id"]
                    to_id = rec["to_id"]
                    from_name = rec["from_name"]
                    to_name = rec["to_name"]
                    from_lon = rec["from_lon"]
                    from_lat = rec["from_lat"]
                    to_lon = rec["to_lon"]
                    to_lat = rec["to_lat"]

                    # Self-link logique : même hut_id des deux côtés
                    if from_id == to_id:
                        print(
                            f"- SELF-LINK détecté: {from_name} (#{from_id}) -> {to_name} (#{to_id}), suppression du/des lien(s)…"
                        )
                        deleted_count = delete_self_link(session, from_id)
                        print(f"  -> {deleted_count} relation(s) supprimée(s)")
                        continue

                    print(f"- ORS: {from_name} (#{from_id}) -> {to_name} (#{to_id})")

                    try:
                        result = call_ors(
                            from_lon,
                            from_lat,
                            to_lon,
                            to_lat,
                            from_name=from_name,
                            to_name=to_name,
                        )
                    except RuntimeError as e:
                        # Cas typique : quota ORS dépassé (429)
                        print(f"  -> arrêt du script: {e}")
                        return

                    if result is None:
                        print("  -> échec ORS, on marque le lien comme ors_skip et on passe au suivant.")
                        mark_link_as_failed(
                            session,
                            from_id,
                            to_id,
                            "ORS error (voir logs du script)",
                        )
                        continue

                    distance_km, dplus_m, dminus_m, geometry_polyline = result

                    updated_count = update_link_in_neo4j(
                        session,
                        from_id,
                        to_id,
                        distance_km,
                        dplus_m,
                        dminus_m,
                        geometry_polyline,
                    )

                    print(
                        f"  OK: {updated_count} lien(s) mis à jour, "
                        f"{distance_km:.2f} km, "
                        f"D+={math.floor(dplus_m)} m, "
                        f"D-={math.floor(dminus_m)} m, "
                        f"len(polyline)={len(geometry_polyline) if geometry_polyline else 0}"
                    )

                    time.sleep(SLEEP_BETWEEN_CALLS)
    finally:
        driver.close()


if __name__ == "__main__":
    main()

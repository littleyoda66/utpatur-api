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
NEO4J_USER = os.environ["NEO4J_USERNAME"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]

ORS_API_KEY = os.environ.get("ORS_API_KEY")
if not ORS_API_KEY:
    raise RuntimeError(
        "Variable d'environnement ORS_API_KEY non définie. "
        "Définis-la (par ex. dans .env) avant de lancer le script."
    )

# Profil rando
ORS_URL = "https://api.openrouteservice.org/v2/directions/foot-hiking"

# Batchs pour éviter de flinguer le quota ORS
BATCH_SIZE = 20
SLEEP_BETWEEN_CALLS = 2.0  # secondes


# -------------------------------------------------------------------
# Connexion Neo4j
# -------------------------------------------------------------------
def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


# -------------------------------------------------------------------
# Récupérer les liens à enrichir (IDs + coords)
# -------------------------------------------------------------------
def fetch_links_to_enrich(session, limit=BATCH_SIZE):
    """
    Récupère des liens (a:Hut)-[l:LINK]->(b:Hut) où l.distance_km IS NULL
    avec IDs, noms et coordonnées.

    On s'appuie sur la propriété a.hut_id / b.hut_id (type entier).
    """
    query = """
    MATCH (a:Hut)-[l:LINK]->(b:Hut)
    WHERE l.distance_km IS NULL
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
# Appel ORS pour une paire de huts
# -------------------------------------------------------------------
def call_ors(from_lon, from_lat, to_lon, to_lat, from_name="?", to_name="?"):
    """
    Appelle ORS entre deux points [lon, lat].
    Retourne (distance_km, dplus_m, dminus_m) ou None en cas d'erreur.
    """
    body = {
        "coordinates": [
            [from_lon, from_lat],  # ORS = [lon, lat]
            [to_lon, to_lat],
        ],
        "elevation": True,
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

    summary = None

    # Format GeoJSON (features)
    if isinstance(data, dict) and "features" in data:
        try:
            feature = data["features"][0]
            props = feature.get("properties", {})
            summary = props.get("summary", {})
        except (KeyError, IndexError, TypeError) as e:
            print(f"  ERREUR parsing 'features' pour {from_name} -> {to_name}: {e}")
            return None

    # Ancien format JSON (routes)
    elif isinstance(data, dict) and "routes" in data:
        try:
            route = data["routes"][0]
            summary = route.get("summary", {})
        except (KeyError, IndexError, TypeError) as e:
            print(f"  ERREUR parsing 'routes' pour {from_name} -> {to_name}: {e}")
            return None

    else:
        print(f"  Réponse ORS inattendue pour {from_name} -> {to_name}: ni 'features' ni 'routes'")
        return None

    try:
        distance_m = float(summary["distance"])
        ascent = float(summary.get("ascent", 0.0))
        descent = float(summary.get("descent", 0.0))
    except (KeyError, ValueError, TypeError) as e:
        print(f"  ERREUR lecture summary pour {from_name} -> {to_name}: {e}")
        return None

    distance_km = distance_m / 1_000.0
    return distance_km, ascent, descent


# -------------------------------------------------------------------
# Mise à jour d'un lien en base, via hut_id
# -------------------------------------------------------------------
def update_link_in_neo4j(session, from_id, to_id, distance_km, dplus_m, dminus_m):
    """
    Met à jour le lien (a)-[l:LINK]->(b) identifié par les hut_id.
    On suppose que a.hut_id / b.hut_id sont uniques.
    """
    query = """
    MATCH (a:Hut {hut_id: $from_id}),
          (b:Hut {hut_id: $to_id})
    MATCH (a)-[l:LINK]->(b)
    SET l.distance_km = $distance_km,
        l.dplus_m     = $dplus_m,
        l.dminus_m    = $dminus_m
    RETURN id(l) AS link_id
    """
    result = session.run(
        query,
        from_id=from_id,
        to_id=to_id,
        distance_km=float(distance_km),
        dplus_m=float(dplus_m),
        dminus_m=float(dminus_m),
    )
    record = result.single()
    return record["link_id"] if record else None


# -------------------------------------------------------------------
# Programme principal
# -------------------------------------------------------------------
def main():
    driver = get_driver()
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

                print(f"- ORS: {from_name} (#{from_id}) -> {to_name} (#{to_id})")

                result = call_ors(
                    from_lon,
                    from_lat,
                    to_lon,
                    to_lat,
                    from_name=from_name,
                    to_name=to_name,
                )

                if result is None:
                    print("  -> échec ORS, on passe au suivant.")
                    continue

                distance_km, dplus_m, dminus_m = result

                link_id = update_link_in_neo4j(
                    session,
                    from_id,
                    to_id,
                    distance_km,
                    dplus_m,
                    dminus_m,
                )

                print(
                    f"  OK: link_id={link_id}, "
                    f"{distance_km:.2f} km, "
                    f"D+={math.floor(dplus_m)} m, "
                    f"D-={math.floor(dminus_m)} m"
                )

                time.sleep(SLEEP_BETWEEN_CALLS)

    driver.close()


if __name__ == "__main__":
    main()

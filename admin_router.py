import os
import re
import json
from typing import List, Optional, Dict, Any, Set

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from huts_router import driver
from admin_security import require_admin


# -------------------------------------------------------------------
# Configuration (variables d'environnement)
# -------------------------------------------------------------------

ORS_API_KEY = os.getenv("ORS_API_KEY")
ORS_BASE_URL = os.getenv("ORS_BASE_URL", "https://api.openrouteservice.org")
OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")


# -------------------------------------------------------------------
# Modèles Pydantic
# -------------------------------------------------------------------

class AdminHutCandidate(BaseModel):
    """Cabane existante dans AuraDB (pour fuzzy-search admin)."""
    hut_id: int
    name: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    country_code: Optional[str] = None


class OverpassHutCandidate(BaseModel):
    """Candidat OSM trouvé via Overpass."""
    osm_id: int
    name: str
    latitude: float
    longitude: float
    country_code: Optional[str] = None
    raw_tags: Optional[Dict[str, Any]] = None


class PreviewRouteRequest(BaseModel):
    from_lat: float
    from_lon: float
    to_lat: float
    to_lon: float


class PreviewRouteResponse(BaseModel):
    distance_km: float
    dplus_m: float
    dminus_m: float


class CreateLinkRequest(BaseModel):
    from_hut_id: int
    to_hut_id: int
    distance_km: float
    dplus_m: float
    dminus_m: float
    bidirectional: bool = False


class CreateLinkResponse(BaseModel):
    created_forward: bool
    created_backward: bool


class ImportHutFromOverpassRequest(BaseModel):
    """Payload pour créer une cabane AuraDB à partir d’un résultat Overpass."""
    name: str
    latitude: float
    longitude: float
    country_code: Optional[str] = None
    osm_id: Optional[int] = None
    raw_tags: Optional[Dict[str, Any]] = None


# -------------------------------------------------------------------
# Router admin (toutes les routes protégées par require_admin)
# -------------------------------------------------------------------

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


# -------------------------------------------------------------------
# 1. Fuzzy search de cabanes existantes dans AuraDB
# -------------------------------------------------------------------

@router.get("/huts/fuzzy-search", response_model=List[AdminHutCandidate])
async def fuzzy_search_huts(
    query: str = Query(..., min_length=2, description="Texte à chercher dans le nom de la cabane"),
    limit: int = Query(20, ge=1, le=100),
):
    """
    Cherche des Huts existants dont le nom contient le texte donné (insensible à la casse).
    Sert à aider l’admin à identifier la bonne cabane à partir d’un nom approximatif.
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
    ORDER BY h.name
    LIMIT $limit
    """

    with driver.session() as session:
        result = session.run(cypher, q=query, limit=limit)
        rows = [record.data() for record in result]

    return rows


# -------------------------------------------------------------------
# 2. Recherche de cabanes / hébergements via Overpass (Laponie)
# -------------------------------------------------------------------

@router.get("/huts/overpass-search", response_model=List[OverpassHutCandidate])
async def overpass_search_huts(
    query: str = Query(..., min_length=2, description="Texte à chercher dans le nom OSM"),
    limit: int = Query(20, ge=1, le=100),
):
    """
    Cherche des cabanes / hébergements dans OSM via Overpass, restreint à la
    Laponie (Suède + Norvège) au nord du cercle polaire.

    Stratégie :

      1) Chercher des lieux (place=hamlet|village|locality) dont le name matche
         dans la bounding box Laponie.
      2) Pour chaque lieu trouvé, chercher dans un rayon de 1 km :
           - tourism=alpine_hut|hostel|guest_house|hotel|chalet|camp_site
           - amenity=shelter
         (nodes + ways, avec out center)
      3) Si aucun lieu ou aucun hébergement trouvé :
           - fallback : recherche par name sur les mêmes tags,
             toujours limitée à la même bounding box.

    IMPORTANT : en cas d’erreur Overpass (timeout, 5xx, JSON invalide…),
    on renvoie simplement [] plutôt qu’une erreur 502, pour ne pas casser le panneau admin.
    """
    if not OVERPASS_URL:
        # vraie erreur de config
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OVERPASS_URL non configuré",
        )

    # Approx Laponie SE/NO (nord du cercle polaire)
    SOUTH, WEST, NORTH, EAST = 66.0, 11.0, 71.5, 32.0

    pattern = re.escape(query)
    candidates: List[OverpassHutCandidate] = []
    seen_ids: Set[str] = set()  # "type/id"

    def element_to_candidate(el: Dict[str, Any]) -> Optional[OverpassHutCandidate]:
        """Transforme un élément Overpass (node/way/relation) en OverpassHutCandidate."""
        osm_type = el.get("type")
        if osm_type not in ("node", "way", "relation"):
            return None

        tags = el.get("tags", {}) or {}
        name = tags.get("name")
        if not name:
            return None

        lat = el.get("lat")
        lon = el.get("lon")
        if lat is None or lon is None:
            center = el.get("center") or {}
            lat = center.get("lat")
            lon = center.get("lon")
        if lat is None or lon is None:
            return None

        country = (
            tags.get("addr:country")
            or tags.get("is_in:country")
            or None
        )

        osm_id = el.get("id")
        return OverpassHutCandidate(
            osm_id=osm_id,
            name=name,
            latitude=lat,
            longitude=lon,
            country_code=country,
            raw_tags=tags,
        )

    def safe_overpass(query_str: str, context: str) -> Optional[Dict[str, Any]]:
        """
        Appelle Overpass et renvoie du JSON ou None en cas de problème,
        sans lever d’erreur HTTP.
        """
        try:
            resp = requests.post(
                OVERPASS_URL,
                data={"data": query_str},
                timeout=25,
            )
        except requests.RequestException as exc:
            print(f"[OVERPASS {context}] Request error: {exc}")
            return None

        if not resp.ok:
            print(f"[OVERPASS {context}] HTTP {resp.status_code}: {resp.text[:300]}")
            return None

        try:
            return resp.json()
        except ValueError:
            print(f"[OVERPASS {context}] Invalid JSON response")
            return None

    # ------------------------------------------------------------
    # Étape 1 : lieux (place=*) par name, dans la bbox Laponie
    # ------------------------------------------------------------
    places_query = f"""
    [out:json][timeout:20];
    node["place"~"hamlet|village|locality"]["name"~"{pattern}", i]({SOUTH},{WEST},{NORTH},{EAST});
    out center 5;
    """
    places_data = safe_overpass(places_query, "places")
    places: List[Dict[str, Any]] = []

    if places_data:
        for el in places_data.get("elements", []):
            if el.get("type") != "node":
                continue
            plat = el.get("lat")
            plon = el.get("lon")
            if plat is None or plon is None:
                center = el.get("center") or {}
                plat = center.get("lat")
                plon = center.get("lon")
            if plat is None or plon is None:
                continue
            places.append({"lat": plat, "lon": plon})

    # ------------------------------------------------------------
    # Étape 2 : pour chaque lieu, hébergements à 1 km
    # ------------------------------------------------------------
    for place in places[:3]:
        if len(candidates) >= limit:
            break

        plat = place["lat"]
        plon = place["lon"]

        around_query = f"""
        [out:json][timeout:20];
        (
          node["tourism"~"alpine_hut|hostel|guest_house|hotel|chalet|camp_site"](around:1000,{plat},{plon});
          way["tourism"~"alpine_hut|hostel|guest_house|hotel|chalet|camp_site"](around:1000,{plat},{plon});
          node["amenity"="shelter"](around:1000,{plat},{plon});
          way["amenity"="shelter"](around:1000,{plat},{plon});
        );
        out center;
        """
        around_data = safe_overpass(around_query, "around")
        if not around_data:
            continue

        for el in around_data.get("elements", []):
            osm_type = el.get("type")
            osm_id = el.get("id")
            key = f"{osm_type}/{osm_id}"
            if key in seen_ids:
                continue

            candidate = element_to_candidate(el)
            if not candidate:
                continue

            candidates.append(candidate)
            seen_ids.add(key)

            if len(candidates) >= limit:
                break

    # ------------------------------------------------------------
    # Étape 3 : fallback par name dans la même bbox (si rien trouvé)
    # ------------------------------------------------------------
    if not candidates:
        direct_query = f"""
        [out:json][timeout:20];
        (
          node["tourism"~"alpine_hut|hostel|guest_house|hotel|chalet|camp_site"]["name"~"{pattern}", i]({SOUTH},{WEST},{NORTH},{EAST});
          way["tourism"~"alpine_hut|hostel|guest_house|hotel|chalet|camp_site"]["name"~"{pattern}", i]({SOUTH},{WEST},{NORTH},{EAST});
          node["amenity"="shelter"]["name"~"{pattern}", i]({SOUTH},{WEST},{NORTH},{EAST});
          way["amenity"="shelter"]["name"~"{pattern}", i]({SOUTH},{WEST},{NORTH},{EAST});
        );
        out center {limit};
        """
        direct_data = safe_overpass(direct_query, "fallback")
        if direct_data:
            for el in direct_data.get("elements", []):
                osm_type = el.get("type")
                osm_id = el.get("id")
                key = f"{osm_type}/{osm_id}"
                if key in seen_ids:
                    continue

                candidate = element_to_candidate(el)
                if not candidate:
                    continue

                candidates.append(candidate)
                seen_ids.add(key)

                if len(candidates) >= limit:
                    break

    # Jamais d’exception ici : au pire, on renvoie []
    return candidates


# -------------------------------------------------------------------
# 3. Import d’une cabane Overpass → Hut AuraDB
# -------------------------------------------------------------------

@router.post(
    "/huts/import-from-overpass",
    response_model=AdminHutCandidate,
    status_code=status.HTTP_201_CREATED,
)
async def import_hut_from_overpass(body: ImportHutFromOverpassRequest):
    """
    Crée un node Hut dans AuraDB à partir d’une cabane Overpass.

    - Génère un nouveau hut_id = max(h.hut_id) + 1
    - Stocke les tags OSM dans h.tags_json
    - Récupère quelques champs utiles (tourism, amenity, operator...)
    """
    with driver.session() as session:
        # Générer un nouvel hut_id
        result = session.run(
            "MATCH (h:Hut) RETURN coalesce(max(h.hut_id), 0) AS max_id"
        )
        record = result.single()
        max_id = record["max_id"] if record and record["max_id"] is not None else 0
        new_id = int(max_id) + 1

        tags = body.raw_tags or {}
        tourism = tags.get("tourism")
        amenity = tags.get("amenity")
        shelter_type = tags.get("shelter_type")
        operator = tags.get("operator")
        tags_json = json.dumps(tags) if tags else None

        session.run(
            """
            CREATE (h:Hut {
                hut_id:       $hut_id,
                name:         $name,
                latitude:     $lat,
                longitude:    $lon,
                country_code: $country,
                osm_id:       $osm_id,
                tourism:      $tourism,
                amenity:      $amenity,
                shelter_type: $shelter_type,
                operator:     $operator,
                tags_json:    $tags_json
            })
            """,
            hut_id=new_id,
            name=body.name,
            lat=body.latitude,
            lon=body.longitude,
            country=body.country_code,
            osm_id=body.osm_id,
            tourism=tourism,
            amenity=amenity,
            shelter_type=shelter_type,
            operator=operator,
            tags_json=tags_json,
        )

    return AdminHutCandidate(
        hut_id=new_id,
        name=body.name,
        latitude=body.latitude,
        longitude=body.longitude,
        country_code=body.country_code,
    )


# -------------------------------------------------------------------
# 4. Prévisualisation d’un segment via OpenRouteService
# -------------------------------------------------------------------

@router.post("/links/preview-route", response_model=PreviewRouteResponse)
async def preview_route(body: PreviewRouteRequest):
    """
    Utilise OpenRouteService (profil foot-hiking) pour proposer
    distance_km, D+, D- entre deux points (lat/lon).

    Nécessite ORS_API_KEY dans les variables d’environnement.
    """
    if not ORS_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ORS_API_KEY non configuré",
        )

    url = ORS_BASE_URL.rstrip("/") + "/v2/directions/foot-hiking"

    payload = {
        "coordinates": [
            [body.from_lon, body.from_lat],
            [body.to_lon, body.to_lat],
        ],
        "elevation": True,
    }

    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Erreur en appelant OpenRouteService: {exc}",
        )

    if not resp.ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"OpenRouteService a renvoyé le statut {resp.status_code}: {resp.text}",
        )

    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Réponse OpenRouteService illisible (JSON invalide)",
        )

    routes = data.get("routes") or []
    if not routes:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OpenRouteService n’a renvoyé aucune route",
        )

    summary = routes[0].get("summary") or {}
    distance_m = summary.get("distance")
    ascent = summary.get("ascent")
    descent = summary.get("descent")

    if distance_m is None or ascent is None or descent is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="La réponse ORS ne contient pas distance / ascent / descent",
        )

    distance_km = distance_m / 1000.0

    return PreviewRouteResponse(
        distance_km=distance_km,
        dplus_m=ascent,
        dminus_m=descent,
    )


# -------------------------------------------------------------------
# 5. Création de segment LINK entre deux Huts
# -------------------------------------------------------------------

@router.post(
    "/links",
    response_model=CreateLinkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_link(body: CreateLinkRequest):
    """
    Crée un segment LINK entre deux Huts existants dans AuraDB.

    - from_hut_id : hut_id de départ
    - to_hut_id   : hut_id d’arrivée
    - distance_km, dplus_m, dminus_m : valeurs (venues d’ORS + éventuellement éditées)
    - bidirectional : si True, crée aussi le segment inverse.
    """
    with driver.session() as session:
        # Segment aller
        result = session.run(
            """
            MATCH (a:Hut {hut_id: $fromId})
            MATCH (b:Hut {hut_id: $toId})
            WITH a, b
            MERGE (a)-[r:LINK]->(b)
            SET r.distance_km = $distance_km,
                r.dplus_m     = $dplus_m,
                r.dminus_m    = $dminus_m
            RETURN id(r) AS rel_id
            """,
            fromId=body.from_hut_id,
            toId=body.to_hut_id,
            distance_km=body.distance_km,
            dplus_m=body.dplus_m,
            dminus_m=body.dminus_m,
        )

        record = result.single()
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Cabane de départ ou d’arrivée introuvable dans AuraDB",
            )

        created_forward = True
        created_backward = False

        # Segment retour si demandé
        if body.bidirectional:
            result_back = session.run(
                """
                MATCH (a:Hut {hut_id: $fromId})
                MATCH (b:Hut {hut_id: $toId})
                WITH a, b
                MERGE (b)-[r:LINK]->(a)
                SET r.distance_km = $distance_km,
                    r.dplus_m     = $dplus_m,
                    r.dminus_m    = $dminus_m
                RETURN id(r) AS rel_id
                """,
                fromId=body.from_hut_id,
                toId=body.to_hut_id,
                distance_km=body.distance_km,
                dplus_m=body.dplus_m,
                dminus_m=body.dminus_m,
            )
            if result_back.single():
                created_backward = True

    return CreateLinkResponse(
        created_forward=created_forward,
        created_backward=created_backward,
    )

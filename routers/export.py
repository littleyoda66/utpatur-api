# routers/export.py
"""Router pour l'export d'itinéraires (KML, GPX, etc.)"""
import logging
import uuid
from datetime import datetime
from typing import List, Optional, Union
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/export", tags=["export"])

# Stockage temporaire des fichiers générés (en production, utiliser Redis ou S3)
kml_storage = {}
gpx_storage = {}


class HutPoint(BaseModel):
    """Point d'une cabane dans l'itinéraire"""
    hut_id: Union[str, int]  # Accepter string ou int
    name: str
    latitude: float
    longitude: float
    country_code: Optional[str] = None
    is_rest_day: bool = False
    
    @field_validator('hut_id', mode='before')
    @classmethod
    def convert_hut_id_to_str(cls, v):
        """Convertir hut_id en string si c'est un int"""
        return str(v) if v is not None else v


class RouteSegment(BaseModel):
    """Segment entre deux cabanes"""
    distance_km: float
    elevation_gain: float  # Accepter float, sera converti en int pour l'affichage
    elevation_loss: float  # Accepter float, sera converti en int pour l'affichage
    day_index: int


class ExportRequest(BaseModel):
    """Requête d'export d'itinéraire"""
    huts: List[HutPoint]
    segments: List[RouteSegment]
    start_date: str  # Format ISO: YYYY-MM-DD
    expedition_name: Optional[str] = "Expédition Laponie"


def generate_kml(request: ExportRequest) -> str:
    """Génère le contenu KML pour l'itinéraire"""
    
    # Construire les coordonnées du tracé
    coordinates = []
    for hut in request.huts:
        coordinates.append(f"{hut.longitude},{hut.latitude},0")
    
    coords_str = " ".join(coordinates)
    
    # Générer les placemarks pour chaque cabane
    placemarks = []
    for i, hut in enumerate(request.huts):
        # Icône selon le type
        if i == 0:
            icon = "https://maps.google.com/mapfiles/kml/paddle/go.png"
            style_id = "start"
        elif i == len(request.huts) - 1:
            icon = "https://maps.google.com/mapfiles/kml/paddle/red-square.png"
            style_id = "end"
        elif hut.is_rest_day:
            icon = "https://maps.google.com/mapfiles/kml/paddle/blu-blank.png"
            style_id = "rest"
        else:
            icon = "https://maps.google.com/mapfiles/kml/paddle/ylw-blank.png"
            style_id = "hut"
        
        # Date de cette étape
        try:
            start = datetime.fromisoformat(request.start_date)
            day_date = start.replace(day=start.day + i)
            date_str = day_date.strftime("%a %d %b")
        except:
            date_str = f"Jour {i}"
        
        # Description avec stats si pas la première cabane
        description_parts = [f"<b>Jour {i}</b> - {date_str}"]
        if hut.country_code:
            country_names = {"NO": "Norvège", "SE": "Suède", "FI": "Finlande"}
            description_parts.append(f"Pays: {country_names.get(hut.country_code.upper(), hut.country_code)}")
        
        if i > 0 and i - 1 < len(request.segments):
            seg = request.segments[i - 1]
            description_parts.append(f"Distance: {seg.distance_km:.1f} km")
            description_parts.append(f"Dénivelé: +{int(seg.elevation_gain)}m / -{int(seg.elevation_loss)}m")
        
        if hut.is_rest_day:
            description_parts.append("<i>Jour de repos</i>")
        
        description = "<br/>".join(description_parts)
        
        placemark = f"""
    <Placemark>
      <name>{hut.name}</name>
      <description><![CDATA[{description}]]></description>
      <styleUrl>#{style_id}</styleUrl>
      <Point>
        <coordinates>{hut.longitude},{hut.latitude},0</coordinates>
      </Point>
    </Placemark>"""
        placemarks.append(placemark)
    
    placemarks_str = "".join(placemarks)
    
    # Calculer les totaux pour la description
    total_distance = sum(s.distance_km for s in request.segments)
    total_gain = int(sum(s.elevation_gain for s in request.segments))
    total_loss = int(sum(s.elevation_loss for s in request.segments))
    num_days = len(request.huts) - 1
    
    # KML complet
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{request.expedition_name}</name>
    <description><![CDATA[
      <b>{request.expedition_name}</b><br/>
      Départ: {request.start_date}<br/>
      Durée: {num_days} jours<br/>
      Distance totale: {total_distance:.1f} km<br/>
      Dénivelé: +{total_gain}m / -{total_loss}m<br/>
      <br/>
      Généré par Ut På Tur
    ]]></description>
    
    <!-- Styles -->
    <Style id="start">
      <IconStyle>
        <Icon><href>https://maps.google.com/mapfiles/kml/paddle/go.png</href></Icon>
        <scale>1.2</scale>
      </IconStyle>
    </Style>
    <Style id="end">
      <IconStyle>
        <Icon><href>https://maps.google.com/mapfiles/kml/paddle/red-square.png</href></Icon>
        <scale>1.2</scale>
      </IconStyle>
    </Style>
    <Style id="hut">
      <IconStyle>
        <Icon><href>https://maps.google.com/mapfiles/kml/paddle/ylw-blank.png</href></Icon>
        <scale>1.0</scale>
      </IconStyle>
    </Style>
    <Style id="rest">
      <IconStyle>
        <Icon><href>https://maps.google.com/mapfiles/kml/paddle/blu-blank.png</href></Icon>
        <scale>0.9</scale>
      </IconStyle>
    </Style>
    <Style id="route">
      <LineStyle>
        <color>ff0066ff</color>
        <width>4</width>
      </LineStyle>
    </Style>
    
    <!-- Tracé de l'itinéraire -->
    <Placemark>
      <name>Itinéraire</name>
      <styleUrl>#route</styleUrl>
      <LineString>
        <tessellate>1</tessellate>
        <altitudeMode>clampToGround</altitudeMode>
        <coordinates>{coords_str}</coordinates>
      </LineString>
    </Placemark>
    
    <!-- Cabanes -->
    {placemarks_str}
    
  </Document>
</kml>"""
    
    return kml


def generate_gpx(request: ExportRequest) -> str:
    """Génère le contenu GPX pour l'itinéraire"""
    
    # Calculer les totaux
    total_distance = sum(s.distance_km for s in request.segments)
    total_gain = int(sum(s.elevation_gain for s in request.segments))
    total_loss = int(sum(s.elevation_loss for s in request.segments))
    num_days = len(request.huts) - 1
    
    # Générer les waypoints pour chaque cabane
    waypoints = []
    for i, hut in enumerate(request.huts):
        # Type de point
        if i == 0:
            sym = "Flag, Green"
            wpt_type = "Départ"
        elif i == len(request.huts) - 1:
            sym = "Flag, Red"
            wpt_type = "Arrivée"
        elif hut.is_rest_day:
            sym = "Campground"
            wpt_type = "Repos"
        else:
            sym = "Lodge"
            wpt_type = "Cabane"
        
        # Date de cette étape
        try:
            start = datetime.fromisoformat(request.start_date)
            from datetime import timedelta
            day_date = start + timedelta(days=i)
            date_str = day_date.strftime("%Y-%m-%d")
        except:
            date_str = ""
        
        # Description
        desc_parts = [f"Jour {i}"]
        if hut.country_code:
            country_names = {"NO": "Norvège", "SE": "Suède", "FI": "Finlande"}
            desc_parts.append(country_names.get(hut.country_code.upper(), hut.country_code))
        if i > 0 and i - 1 < len(request.segments):
            seg = request.segments[i - 1]
            desc_parts.append(f"{seg.distance_km:.1f}km +{int(seg.elevation_gain)}m -{int(seg.elevation_loss)}m")
        if hut.is_rest_day:
            desc_parts.append("Jour de repos")
        
        desc = " | ".join(desc_parts)
        
        waypoint = f"""  <wpt lat="{hut.latitude}" lon="{hut.longitude}">
    <name>{hut.name}</name>
    <desc>{desc}</desc>
    <sym>{sym}</sym>
    <type>{wpt_type}</type>
  </wpt>"""
        waypoints.append(waypoint)
    
    waypoints_str = "\n".join(waypoints)
    
    # Générer le track
    trackpoints = []
    for hut in request.huts:
        trackpoints.append(f'      <trkpt lat="{hut.latitude}" lon="{hut.longitude}"></trkpt>')
    
    trackpoints_str = "\n".join(trackpoints)
    
    # GPX complet
    gpx = f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Ut På Tur - utpatur.app"
  xmlns="http://www.topografix.com/GPX/1/1"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd">
  <metadata>
    <name>{request.expedition_name}</name>
    <desc>{num_days} jours | {total_distance:.1f} km | +{total_gain}m -{total_loss}m</desc>
    <author>
      <name>Ut På Tur</name>
      <link href="https://utpatur.app">
        <text>Ut På Tur - Planificateur de raids en Laponie</text>
      </link>
    </author>
    <time>{datetime.utcnow().isoformat()}Z</time>
  </metadata>
{waypoints_str}
  <trk>
    <name>{request.expedition_name}</name>
    <desc>Itinéraire généré par Ut På Tur</desc>
    <trkseg>
{trackpoints_str}
    </trkseg>
  </trk>
</gpx>"""
    
    return gpx


@router.post("/kml")
async def create_kml(request: ExportRequest):
    """
    Génère un fichier KML pour l'itinéraire et retourne son URL.
    Le KML peut ensuite être ouvert dans Google Earth.
    """
    try:
        # Générer le KML
        kml_content = generate_kml(request)
        
        # Générer un ID unique
        kml_id = str(uuid.uuid4())[:8]
        
        # Stocker temporairement (TTL de 1h en production)
        kml_storage[kml_id] = {
            "content": kml_content,
            "created_at": datetime.utcnow(),
            "expedition_name": request.expedition_name
        }
        
        logger.info(f"KML généré: {kml_id} pour '{request.expedition_name}'")
        
        return {
            "kml_id": kml_id,
            "kml_url": f"/api/v1/export/kml/{kml_id}",
            "google_earth_url": None  # Sera construit côté frontend
        }
        
    except Exception as e:
        logger.error(f"Erreur génération KML: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erreur lors de la génération du KML: {str(e)}"
        )


@router.get("/kml/{kml_id}")
async def get_kml(kml_id: str):
    """
    Récupère un fichier KML précédemment généré.
    """
    if kml_id not in kml_storage:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="KML non trouvé ou expiré"
        )
    
    kml_data = kml_storage[kml_id]
    
    return Response(
        content=kml_data["content"],
        media_type="application/vnd.google-earth.kml+xml",
        headers={
            "Content-Disposition": f'attachment; filename="utpatur-{kml_id}.kml"'
        }
    )


@router.delete("/kml/{kml_id}")
async def delete_kml(kml_id: str):
    """
    Supprime un fichier KML du stockage.
    """
    if kml_id in kml_storage:
        del kml_storage[kml_id]
        return {"deleted": True}
    
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="KML non trouvé"
    )


@router.post("/gpx")
async def create_gpx(request: ExportRequest):
    """
    Génère un fichier GPX pour l'itinéraire et retourne son URL.
    Le GPX peut être importé dans la plupart des applications de navigation.
    """
    try:
        # Générer le GPX
        gpx_content = generate_gpx(request)
        
        # Générer un ID unique
        gpx_id = str(uuid.uuid4())[:8]
        
        # Stocker temporairement (TTL de 1h en production)
        gpx_storage[gpx_id] = {
            "content": gpx_content,
            "created_at": datetime.utcnow(),
            "expedition_name": request.expedition_name
        }
        
        logger.info(f"GPX généré: {gpx_id} pour '{request.expedition_name}'")
        
        return {
            "gpx_id": gpx_id,
            "gpx_url": f"/api/v1/export/gpx/{gpx_id}"
        }
        
    except Exception as e:
        logger.error(f"Erreur génération GPX: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erreur lors de la génération du GPX: {str(e)}"
        )


@router.get("/gpx/{gpx_id}")
async def get_gpx(gpx_id: str):
    """
    Récupère un fichier GPX précédemment généré.
    """
    if gpx_id not in gpx_storage:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="GPX non trouvé ou expiré"
        )
    
    gpx_data = gpx_storage[gpx_id]
    
    return Response(
        content=gpx_data["content"],
        media_type="application/gpx+xml",
        headers={
            "Content-Disposition": f'attachment; filename="utpatur-{gpx_id}.gpx"'
        }
    )


@router.delete("/gpx/{gpx_id}")
async def delete_gpx(gpx_id: str):
    """
    Supprime un fichier GPX du stockage.
    """
    if gpx_id in gpx_storage:
        del gpx_storage[gpx_id]
        return {"deleted": True}
    
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="GPX non trouvé"
    )

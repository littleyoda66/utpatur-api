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

# Stockage temporaire des KML générés (en production, utiliser Redis ou S3)
kml_storage = {}


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

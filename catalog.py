"""
catalog.py — Interrogation du catalogue OData de Copernicus Data Space Ecosystem
pour trouver les produits (images) disponibles selon une zone, une date et une collection.
"""

import logging
import requests

logger = logging.getLogger("satellite_agent.catalog")

CATALOG_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"


def extract_footprint(product: dict):
    """
    Extrait la vraie empreinte géographique (GeoJSON) d'un produit retourné
    par l'API Copernicus. C'est cette forme exacte (pas un rectangle
    approximatif) qui correspond à ce qu'on voit sur le site Copernicus Browser.

    Args:
        product: un dict brut retourné par search_products()

    Returns:
        Le geometry GeoJSON (dict) si disponible, sinon None.
    """
    geo_footprint = product.get("GeoFootprint")
    if geo_footprint and isinstance(geo_footprint, dict) and "coordinates" in geo_footprint:
        return geo_footprint
    return None


def _bbox_to_wkt_polygon(bbox):
    """Convertit [lon_min, lat_min, lon_max, lat_max] en POLYGON WKT."""
    lon_min, lat_min, lon_max, lat_max = bbox
    return (
        f"POLYGON(({lon_min} {lat_min}, {lon_max} {lat_min}, "
        f"{lon_max} {lat_max}, {lon_min} {lat_max}, {lon_min} {lat_min}))"
    )


def search_products(collection, bbox, start_iso, end_iso, product_type=None,
                     max_cloud_cover=None, top=50):
    """
    Recherche les produits correspondant aux critères.

    Args:
        collection: ex. "SENTINEL-2", "SENTINEL-1"
        bbox: [lon_min, lat_min, lon_max, lat_max]
        start_iso / end_iso: bornes de dates au format "YYYY-MM-DDTHH:MM:SS.000Z"
        product_type: ex. "S2MSI2A" (optionnel)
        max_cloud_cover: pourcentage max de nuages (optionnel, Sentinel-2 uniquement)
        top: nombre max de résultats

    Returns:
        Liste de dicts (produits bruts retournés par l'API)
    """
    wkt = _bbox_to_wkt_polygon(bbox)

    filters = [
        f"Collection/Name eq '{collection}'",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{wkt}')",
        f"ContentDate/Start gt {start_iso}",
        f"ContentDate/Start lt {end_iso}",
    ]

    if product_type:
        filters.append(
            "Attributes/OData.CSC.StringAttribute/any("
            f"att:att/Name eq 'productType' and att/OData.CSC.StringAttribute/Value eq '{product_type}')"
        )

    if max_cloud_cover is not None:
        filters.append(
            "Attributes/OData.CSC.DoubleAttribute/any("
            f"att:att/Name eq 'cloudCover' and att/OData.CSC.DoubleAttribute/Value le {max_cloud_cover})"
        )

    filter_str = " and ".join(filters)
    params = {
        "$filter": filter_str,
        "$top": top,
        "$orderby": "ContentDate/Start desc",
    }

    logger.debug(f"Requête catalogue: {params}")

    try:
        response = requests.get(CATALOG_URL, params=params, timeout=60)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Erreur lors de la requête au catalogue: {e}")
        return []

    data = response.json()
    products = data.get("value", [])
    logger.info(f"{len(products)} produit(s) trouvé(s) pour {collection} sur la période demandée.")
    return products
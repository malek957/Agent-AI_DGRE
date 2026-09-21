"""
config.py — Configuration des zones d'intérêt (AOI) et des collections satellites
à surveiller. Modifiez WATCHES pour ajouter/retirer des zones ou des capteurs.
"""

from pathlib import Path

# Chaque "watch" définit une zone géographique + un type de produit à surveiller.
# bbox = [longitude_min, latitude_min, longitude_max, latitude_max]  (WGS84)
#
# Astuce pour trouver une bbox rapidement : https://bboxfinder.com/
from watches_config import WATCHES as GENERATED_WATCHES

# Zones de test initiales (tunis_sentinel1, barrage_sidi_salem) retirées :
# remplacées par les zones réelles DGRE générées depuis les shapefiles.

# Zones radar Sentinel-1, générées à partir des mêmes bbox que les zones
# Sentinel-2 (mêmes gouvernorats, même géométrie) — noms suffixés "_s1"
# pour ne pas entrer en collision avec les zones optiques de même nom.
def _generate_sentinel1_watches():
    result = []
    for w in GENERATED_WATCHES:
        result.append({
            "name": f"{w['name']}_s1",
            "collection": "SENTINEL-1",
            "product_type": "IW_GRDH_1S",
            "bbox": w["bbox"],
            "max_cloud_cover": None,   # non pertinent pour le radar
        })
    return result

WATCHES = GENERATED_WATCHES + _generate_sentinel1_watches()

# ---------------------------------------------------------------------------
# Zones Landsat via Microsoft Planetary Computer (remplace Landsat/USGS direct)
# ---------------------------------------------------------------------------
# MPC_WATCHES généré automatiquement à partir des mêmes 24 gouvernorats
# que WATCHES (Sentinel-2), pour garder des noms et des zones cohérents
# — pas de bbox nationale ni de nom générique.
def _generate_mpc_watches():
    result = []
    for w in GENERATED_WATCHES:
        for satellite in ("landsat8", "landsat9"):
            result.append({
                "name": w["name"],          # même nom que la zone Sentinel-2 (ex: gouvernorats_tunis)
                "satellite": satellite,
                "bbox": w["bbox"],           # même bbox précise du gouvernorat
                "cloud_threshold": 20,
                "max_results": 3,
            })
    return result

MPC_WATCHES = _generate_mpc_watches()

# ---------------------------------------------------------------------------
# Paramètres généraux du pipeline
# ---------------------------------------------------------------------------

# Combien de jours en arrière chercher au premier lancement d'une zone
INITIAL_LOOKBACK_DAYS =60

# Nombre max de résultats par requête à l'API
MAX_RESULTS_PER_QUERY = 3

# Seuil de nuages par défaut si une zone n'en précise pas
DEFAULT_CLOUD_THRESHOLD = 20

# Ratio minimum de pixels valides (non nuageux/non corrompus) pour valider une image
DEFAULT_MIN_VALID_PIXEL_RATIO = 0.6

# Indices spectraux calculés pour chaque image validée
INDICES_TO_COMPUTE = ["NDVI", "NDWI", "MNDWI", "NDMI", "SAVI", "WDVI"]

# Supprimer le fichier .zip source après extraction réussie
# True  = économise l'espace disque (~700 Mo économisés par image)
# False = conserve le .zip (utile si vous voulez re-extraire plus tard)
CLEANUP_ZIP_AFTER_EXTRACTION = True

# ---------------------------------------------------------------------------
# Chemin vers les shapefiles administratifs (utilisé par generate_watches.py
# et par page_carte.py pour afficher les limites gouvernorats/délégations)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
SHAPEFILES_DIR = BASE_DIR / "data" / "shapefiles"
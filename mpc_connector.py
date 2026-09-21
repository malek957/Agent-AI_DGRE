"""
mpc_connector.py — Connecteur Microsoft Planetary Computer pour Landsat 8/9.

POURQUOI MICROSOFT PLANETARY COMPUTER ?
=========================================
Microsoft Planetary Computer (MPC) héberge les mêmes données Landsat que
l'USGS, publiées par l'USGS lui-même, mais accessibles via une API STAC
publique sans compte ni token requis. C'est la solution alternative quand
l'API M2M USGS est bloquée par le réseau.

COMMENT ÇA FONCTIONNE ?
=========================
1. RECHERCHE via API STAC publique
   → https://planetarycomputer.microsoft.com/api/stac/v1
   → On cherche les scènes Landsat disponibles sur la Tunisie
   → Chaque scène a des "assets" (liens directs vers chaque bande)

2. SIGNATURE des URLs (SAS Azure)
   → MPC utilise Azure Blob Storage avec des URLs signées temporaires
   → Un endpoint public /api/sas/v1/sign génère la signature automatiquement
   → Aucun compte Azure requis

3. TÉLÉCHARGEMENT bande par bande
   → Contrairement à USGS (archive .tar complète de 1-2 Go),
     MPC permet de télécharger uniquement les bandes nécessaires
   → Économie de bande passante et d'espace disque

BANDES DISPONIBLES (alias harmonisés avec Sentinel-2 dans indices.py) :
   B02 → blue    (482nm)
   B03 → green   (562nm)   ← NDWI, MNDWI
   B04 → red     (655nm)   ← NDVI, SAVI, WDVI
   B08 → nir08   (865nm)   ← NDVI, NDWI, NDMI
   B11 → swir16  (1609nm)  ← MNDWI, NDMI
   B12 → swir22  (2201nm)
   QA  → qa_pixel          ← masque nuages (équivalent SCL Sentinel-2)

RÉSOLUTION : 30 mètres
FRÉQUENCE  : L8 + L9 combinés = ~8 jours sur la Tunisie
"""

import logging
import requests
from pathlib import Path

logger = logging.getLogger("satellite_agent.mpc")

# URL de base de l'API STAC Microsoft Planetary Computer
MPC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Collection Landsat sur MPC
LANDSAT_COLLECTION = "landsat-c2-l2"

# Correspondance alias harmonisé → nom d'asset MPC
# Ces alias sont identiques à ceux utilisés dans indices.py
# → le calcul des indices fonctionne sans aucune modification
BANDS_MAP = {
    "B02": "blue",
    "B03": "green",
    "B04": "red",
    "B08": "nir08",
    "B11": "swir16",
    "B12": "swir22",
    "QA":  "qa_pixel",
}

# Préfixes des IDs de scènes selon le satellite
SATELLITE_PREFIXES = {
    "landsat8": "LC08",
    "landsat9": "LC09",
}


# ===========================================================================
# RECHERCHE
# ===========================================================================

def search_landsat_mpc(satellite: str, bbox: list,
                       date_start: str, date_end: str,
                       max_cloud_cover: int = 80,
                       max_results: int = 20) -> list:
    """
    Recherche les scènes Landsat disponibles sur Microsoft Planetary Computer.

    Paramètres
    ----------
    satellite       : "landsat8" ou "landsat9"
    bbox            : [lon_min, lat_min, lon_max, lat_max]
    date_start      : "YYYY-MM-DD"
    date_end        : "YYYY-MM-DD"
    max_cloud_cover : pré-filtre nuages (%)
    max_results     : nombre max de scènes à retourner

    Retourne
    --------
    Liste de features STAC (dicts) avec métadonnées + liens des bandes
    """
    prefix = SATELLITE_PREFIXES.get(satellite, "LC08")

    logger.info(
        f"Recherche MPC {satellite.upper()} | "
        f"zone={bbox} | {date_start} → {date_end} | nuages≤{max_cloud_cover}%"
    )

    # Requête STAC standard
    payload = {
        "collections": [LANDSAT_COLLECTION],
        "bbox":        bbox,
        "datetime":    f"{date_start}T00:00:00Z/{date_end}T23:59:59Z",
        "query": {
            "eo:cloud_cover": {"lte": max_cloud_cover},
        },
        "limit": max_results,
    }

    try:
        resp = requests.post(
            f"{MPC_STAC_URL}/search",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except requests.exceptions.RequestException as exc:
        logger.error(f"Erreur recherche MPC : {exc}")
        return []

    # Filtrer par préfixe satellite
    scenes = [f for f in features if f.get("id", "").startswith(prefix)]

    # Si filtre trop strict, accepter tous les résultats
    if not scenes and features:
        scenes = features

    logger.info(f"{len(scenes)} scène(s) {satellite.upper()} trouvée(s) sur MPC.")
    return scenes


# ===========================================================================
# SIGNATURE DES URLS (nécessaire pour Azure Blob Storage)
# ===========================================================================

def _sign_url(url: str) -> str:
    """
    Signe l'URL d'un asset MPC pour autoriser le téléchargement depuis Azure.
    L'endpoint de signature est public — aucun compte requis.
    """
    try:
        resp = requests.get(
            "https://planetarycomputer.microsoft.com/api/sas/v1/sign",
            params={"href": url},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("href", url)
    except Exception as exc:
        logger.warning(f"Signature URL échouée ({exc}) — utilisation URL brute")
        return url


# ===========================================================================
# TÉLÉCHARGEMENT
# ===========================================================================

def download_landsat_bands_mpc(scene: dict, dest_dir: Path) -> dict:
    """
    Télécharge les bandes d'une scène Landsat depuis MPC.

    Avantage clé : on télécharge UNIQUEMENT les bandes nécessaires
    (B04, B08, B11, QA...) sans télécharger toute l'archive comme avec USGS.

    Retourne un dict {alias: chemin_local} compatible avec indices.py
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    scene_id = scene.get("id", "unknown")
    assets   = scene.get("assets", {})
    bands    = {}

    for alias, asset_name in BANDS_MAP.items():
        if asset_name not in assets:
            logger.warning(f"Asset '{asset_name}' absent pour {scene_id}")
            continue

        asset_href = assets[asset_name].get("href", "")
        if not asset_href:
            continue

        # Fichier local
        filename   = f"{scene_id}_{alias}.tif"
        local_path = dest_dir / filename

        # Ne pas re-télécharger si déjà présent
        if local_path.exists():
            logger.info(f"Déjà présent : {filename}")
            bands[alias] = str(local_path)
            continue

        # Signer l'URL Azure
        signed_url = _sign_url(asset_href)

        logger.info(f"Téléchargement {alias} ({asset_name})...")
        try:
            with requests.get(signed_url, stream=True, timeout=180) as resp:
                resp.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)

            size_mb = round(local_path.stat().st_size / 1024 / 1024, 1)
            logger.info(f"✅ {filename} ({size_mb} Mo)")
            bands[alias] = str(local_path)

        except Exception as exc:
            logger.error(f"Échec téléchargement bande {alias} : {exc}")

    return bands


# ===========================================================================
# VALIDATION NUAGES (QA_PIXEL)
# ===========================================================================

def compute_cloud_cover_qa(qa_path: str) -> tuple:
    """
    Calcule le taux de nuages depuis le masque QA_PIXEL Landsat.
    Équivalent de la bande SCL pour Sentinel-2 (validator.py).

    Bits testés dans QA_PIXEL :
      Bit 1 = Dilated Cloud
      Bit 3 = Cloud
      Bit 4 = Cloud Shadow

    Retourne (cloud_cover_percent, valid_pixel_ratio)
    """
    try:
        import numpy as np
        import rasterio
    except ImportError:
        logger.error("numpy ou rasterio non installé.")
        return 0.0, 1.0

    try:
        with rasterio.open(qa_path) as src:
            qa = src.read(1).astype("uint16")
    except Exception as exc:
        logger.error(f"Impossible de lire QA_PIXEL ({qa_path}) : {exc}")
        return 0.0, 1.0

    total        = qa.size
    nodata       = (qa == 1)
    valid        = int(total - nodata.sum())

    if valid == 0:
        return 100.0, 0.0

    cloud_mask = (
        ((qa >> 1) & 1).astype(bool) |   # Dilated cloud
        ((qa >> 3) & 1).astype(bool) |   # Cloud
        ((qa >> 4) & 1).astype(bool)     # Cloud shadow
    )
    cloud_pixels = int((cloud_mask & ~nodata).sum())
    cloud_pct    = float(100.0 * cloud_pixels / valid)
    valid_ratio  = float(valid / total)

    logger.debug(
        f"QA Landsat : nuages={cloud_pct:.1f}% | "
        f"pixels valides={valid_ratio:.1%}"
    )
    return cloud_pct, valid_ratio


# ===========================================================================
# FONCTION PRINCIPALE — appelée par agent.py
# ===========================================================================

def process_landsat_mpc_watch(watch: dict,
                               date_start: str,
                               date_end: str,
                               destination_dir: Path,
                               cloud_threshold: float = 20.0) -> list:
    """
    Traite un watch Landsat complet via Microsoft Planetary Computer.

    Pipeline :
      1. Recherche des scènes disponibles (STAC)
      2. Téléchargement des bandes nécessaires (Azure COG)
      3. Calcul du taux de nuages (QA_PIXEL)
      4. Décision : validée ou rejetée

    Retourne une liste de dicts au même format que validate_product()
    de validator.py — compatible avec indices.py et file_storage.py.
    """
    satellite = watch.get("satellite", "landsat8")
    bbox      = watch.get("bbox", [7.49, 30.18, 11.60, 37.55])

    # 1. Recherche
    scenes = search_landsat_mpc(
        satellite=satellite,
        bbox=bbox,
        date_start=date_start,
        date_end=date_end,
        max_cloud_cover=int(min(cloud_threshold * 3, 100)),
        max_results=watch.get("max_results", 10),
    )

    if not scenes:
        return []

    results = []

    for scene in scenes:
        scene_id       = scene.get("id", "unknown")
        props          = scene.get("properties", {})
        cloud_provider = props.get("eo:cloud_cover")
        content_date   = props.get("datetime", "")[:10]
        scene_dir      = destination_dir / scene_id

        logger.info(f"\n--- Scène MPC : {scene_id} ---")
        logger.info(f"    Date : {content_date} | Nuages : {cloud_provider}%")

        # 2. Téléchargement des bandes
        bands = download_landsat_bands_mpc(scene, scene_dir)

        if not bands:
            results.append({
                "scene_id":             scene_id,
                "source":               "mpc",
                "satellite":            satellite,
                "content_date":         content_date,
                "cloud_cover_provider": cloud_provider,
                "cloud_cover_scl":      None,
                "valid_pixel_ratio":    None,
                "status":               "failed",
                "rejection_reason":     "Aucune bande téléchargée",
                "safe_dir":             str(scene_dir),
                "bands":                {},
            })
            continue

        # 3. Calcul nuages via QA_PIXEL
        if "QA" in bands:
            cloud_pct, valid_ratio = compute_cloud_cover_qa(bands["QA"])
        else:
            cloud_pct   = float(cloud_provider) if cloud_provider else 0.0
            valid_ratio = 1.0
            logger.warning("QA absent — utilisation valeur fournisseur")

        # 4. Décision
        if cloud_pct > cloud_threshold:
            status = "rejected_cloud"
            reason = (
                f"Nuages QA={cloud_pct:.1f}% > seuil {cloud_threshold}%"
            )
            logger.warning(f"REJETÉE : {scene_id} — {reason}")
        else:
            status = "validated"
            reason = None
            logger.info(
                f"VALIDÉE : {scene_id} | "
                f"nuages={cloud_pct:.1f}% | "
                f"pixels valides={valid_ratio:.1%}"
            )

        results.append({
            "scene_id":             scene_id,
            "source":               "mpc",
            "satellite":            satellite,
            "content_date":         content_date,
            "cloud_cover_provider": cloud_provider,
            "cloud_cover_scl":      cloud_pct,
            "valid_pixel_ratio":    valid_ratio,
            "status":               status,
            "rejection_reason":     reason,
            "safe_dir":             str(scene_dir),
            "bands":                bands,
        })

    return results

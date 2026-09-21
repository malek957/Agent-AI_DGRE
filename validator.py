"""
validator.py — Extraction des archives .SAFE et validation qualité des images.

POURQUOI CE MODULE EXISTE-T-IL ?
=================================
Quand vous téléchargez une image depuis Copernicus Data Space, vous récupérez
un fichier .zip contenant un dossier au format .SAFE (Standard Archive Format
for Europe). Ce format contient :
  - les bandes spectrales en .jp2 (JPEG2000), organisées par résolution
  - des métadonnées XML
  - la bande SCL (Scene Classification Layer) — notre outil clé pour les nuages

Le problème des métadonnées fournisseur
---------------------------------------
L'API Copernicus renvoie déjà un champ "cloudCover" dans ses métadonnées.
Alors pourquoi recalculer ? Parce que ce pourcentage est calculé sur
l'emprise COMPLÈTE de la tuile (110km × 110km). Une tuile annoncée à
"15% de nuages" peut avoir son unique nuage exactement au-dessus du
barrage Sidi Salem — votre zone d'intérêt — et être complètement inutilisable.

Notre approche : on lit la bande SCL pixel par pixel sur la zone extraite
et on calcule le vrai taux de nuages sur CE QU'ON VOIT, pas sur la tuile entière.

La bande SCL (Sen2Cor, ESA)
----------------------------
Chaque pixel reçoit un code entier de 0 à 11 :
  0  = NO_DATA (hors emprise)
  1  = DEFECTIVE (pixel saturé/défectueux)
  2  = DARK_AREA_PIXELS (ombres topographiques)
  3  = CLOUD_SHADOWS      ← compté comme problématique
  4  = VEGETATION         ← valide
  5  = NOT_VEGETATED      ← valide
  6  = WATER              ← valide (important pour nous !)
  7  = UNCLASSIFIED       ← valide
  8  = CLOUD_MEDIUM_PROB  ← compté comme nuage
  9  = CLOUD_HIGH_PROB    ← compté comme nuage
  10 = THIN_CIRRUS        ← compté comme nuage (voile nuageux)
  11 = SNOW               ← valide

On inclut les ombres de nuages (code 3) car elles faussent aussi les
indices spectraux (NDVI, NDWI...) calculés à l'étape suivante.

Structure du résultat
---------------------
La fonction principale `validate_product` retourne un dict avec tous les
éléments nécessaires à l'étape suivante (calcul des indices) :
  {
    "status": "validated" | "rejected_cloud" | "rejected_corrupt" | "failed",
    "safe_dir": chemin vers le dossier .SAFE extrait,
    "bands": {"B02": path, "B03": path, ...},  # chemins des bandes extraites
    "cloud_cover_scl": 12.4,       # % calculé sur la SCL
    "cloud_cover_provider": 8.0,   # % annoncé par Copernicus (pour comparaison)
    "valid_pixel_ratio": 0.87,     # proportion de pixels non-nodata
    "rejection_reason": None | "...",
  }
"""

import os
import re
import zipfile
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("satellite_agent.validator")

# ---------------------------------------------------------------------------
# Codes SCL considérés comme "mauvais" (nuages ou artefacts liés aux nuages)
# ---------------------------------------------------------------------------
SCL_CLOUD_CODES = {3, 8, 9, 10}   # ombres de nuages + nuages moyens/épais/cirrus
SCL_NODATA_CODES = {0, 1}          # hors emprise ou pixel défectueux

# Résolution de la bande SCL disponible dans le produit L2A
# La SCL existe à 20m et 60m. On utilise 20m pour plus de précision.
SCL_RESOLUTION = "R20m"

# Bandes spectrales à extraire pour les indices (alias -> motif de nom de fichier)
# Ces alias correspondent exactement à ceux utilisés dans indices.py
BANDS_TO_EXTRACT = {
    "B02": "B02",   # Bleu  (10m) — utilisé pour EVI
    "B03": "B03",   # Vert  (10m) — utilisé pour NDWI, MNDWI
    "B04": "B04",   # Rouge (10m) — utilisé pour NDVI, SAVI, WDVI, EVI
    "B08": "B08",   # NIR   (10m) — utilisé pour NDVI, NDWI, NDMI, SAVI, WDVI
    "B8A": "B8A",   # NIR étroit (20m)
    "B11": "B11",   # SWIR1 (20m) — utilisé pour MNDWI, NDMI
    "B12": "B12",   # SWIR2 (20m)
    "SCL": "SCL",   # Scene Classification Layer (20m) — masque nuages
}


# ===========================================================================
# FONCTION PRINCIPALE
# ===========================================================================

def validate_product(zip_path: str, cloud_threshold: float,
                     min_valid_pixel_ratio: float = 0.5,
                     cloud_cover_provider: float = None,
                     collection: str = None) -> dict:
    """
    Extrait une archive .zip Sentinel-2 L2A et valide la qualité de l'image.

    Paramètres
    ----------
    zip_path : str
        Chemin vers le fichier .zip téléchargé (ex: data/SENTINEL-2/tunis/S2A_....zip)
    cloud_threshold : float
        Pourcentage maximal de nuages accepté (ex: 20.0 pour 20%)
    min_valid_pixel_ratio : float
        Proportion minimale de pixels valides (non-nodata) exigée (ex: 0.5)
    cloud_cover_provider : float | None
        Pourcentage de nuages annoncé par Copernicus (pour info/comparaison)

    Retourne
    --------
    dict avec les clés :
        status            : "validated" | "rejected_cloud" | "rejected_corrupt" | "failed"
        safe_dir          : chemin vers le dossier .SAFE extrait (str)
        bands             : dict {alias: path} des bandes extraites (str)
        cloud_cover_scl   : % nuages recalculé localement (float)
        cloud_cover_provider : % nuages Copernicus (float | None)
        valid_pixel_ratio : ratio pixels valides (float)
        rejection_reason  : texte explicatif si rejeté (str | None)
    """
    result = {
        "status": "failed",
        "safe_dir": None,
        "bands": {},
        "cloud_cover_scl": None,
        "cloud_cover_provider": cloud_cover_provider,
        "valid_pixel_ratio": None,
        "rejection_reason": None,
        "preview_png": None,
    }

    # --- 1. Vérification de l'archive ---
    if not os.path.exists(zip_path):
        result["rejection_reason"] = f"Archive introuvable : {zip_path}"
        logger.error(result["rejection_reason"])
        return result

    if not zipfile.is_zipfile(zip_path):
        result["status"] = "rejected_corrupt"
        result["rejection_reason"] = f"Archive corrompue (pas un ZIP valide) : {zip_path}"
        logger.error(result["rejection_reason"])
        return result

    # --- 2. Extraction ---
    extract_dir = Path(zip_path).parent / "extracted"
    safe_dir = _extract_safe(zip_path, extract_dir)
    if safe_dir is None:
        result["status"] = "rejected_corrupt"
        result["rejection_reason"] = "Impossible d'extraire le dossier .SAFE"
        return result

    
    result["safe_dir"] = str(safe_dir)  
    logger.info(f"Archive extraite dans : {safe_dir}")

    # Aperçu PNG natif fourni par l'ESA (déjà prêt, pas besoin de le regénérer)
    preview = _find_preview_png(safe_dir)
    if preview:
        result["preview_png"] = str(preview)
        logger.info(f"Aperçu PNG trouvé : {preview}")

    # --- 2bis. Cas particulier : Sentinel-1 (radar) ---
    # Pas de bandes optiques, pas de SCL, pas de notion de nuages pour le radar.
    # L'extraction réussie du .SAFE suffit à considérer le produit comme valide.
    if collection == "SENTINEL-1":
        result["status"] = "validated"
        result["rejection_reason"] = None
        result["cloud_cover_scl"] = None
        result["valid_pixel_ratio"] = 1.0
        logger.info(
            f"VALIDÉE (Sentinel-1 radar, pas de validation optique applicable) : "
            f"{Path(zip_path).name}"
        )
        return result

    # --- 3. Localisation des bandes ---
    bands = _find_bands(safe_dir)
    result["bands"] = bands

    if not bands:
        result["status"] = "rejected_corrupt"
        result["rejection_reason"] = "Aucune bande spectrale trouvée dans le .SAFE"
        logger.error(result["rejection_reason"])
        return result

    logger.info(f"Bandes trouvées : {list(bands.keys())}")

    # --- 4. Lecture de la SCL et calcul du taux de nuages ---
    if "SCL" not in bands:
        # Pas de SCL = probablement pas un produit L2A (ex: L1C)
        # On accepte quand même l'image en indiquant l'absence de masque nuages
        logger.warning("Bande SCL introuvable — validation nuages ignorée (produit non-L2A ?)")
        result["status"] = "validated"
        result["cloud_cover_scl"] = None
        result["valid_pixel_ratio"] = 1.0
        return result

    cloud_pct, valid_ratio = _compute_cloud_cover_from_scl(bands["SCL"])
    result["cloud_cover_scl"] = cloud_pct
    result["valid_pixel_ratio"] = valid_ratio

    # --- 5. Information seulement — AUCUN rejet basé sur les nuages ou
    # les pixels valides. Sur demande explicite : toute image extraite
    # avec succès est acceptée ; on se contente de logger le taux de
    # nuages et de pixels valides à titre indicatif.
    if valid_ratio < min_valid_pixel_ratio:
        logger.info(
            f"ℹ️  Pixels valides sous le seuil indicatif : {valid_ratio:.1%} "
            f"(seuil indicatif : {min_valid_pixel_ratio:.1%}) — image conservée."
        )

    if cloud_pct > cloud_threshold:
        logger.info(
            f"ℹ️  Couverture nuageuse au-dessus du seuil indicatif : {cloud_pct:.1f}% "
            f"(seuil indicatif : {cloud_threshold}%) — image conservée."
        )

    result["status"] = "validated"
    logger.info(
        f"VALIDÉE : {Path(zip_path).name} | "
        f"nuages SCL={cloud_pct:.1f}% (fournisseur={cloud_cover_provider}%) | "
        f"pixels valides={valid_ratio:.1%}"
    )
    return result

    # Tout est bon
    result["status"] = "validated"
    logger.info(
        f"VALIDÉE : {Path(zip_path).name} | "
        f"nuages SCL={cloud_pct:.1f}% (fournisseur={cloud_cover_provider}%) | "
        f"pixels valides={valid_ratio:.1%}"
    )
    return result


# ===========================================================================
# FONCTIONS INTERNES
# ===========================================================================

def _extract_safe(zip_path: str, extract_dir: Path) -> Path | None:
    """
    Extrait le .zip et retourne le chemin du dossier .SAFE extrait.

    IMPORTANT — CORRECTION D'UN BUG :
    Un produit Sentinel contient exactement un dossier racine .SAFE à
    l'intérieur du zip. On détermine son NOM À PARTIR DU CONTENU DE
    L'ARCHIVE elle-même (zf.namelist()), PAS en regardant ce qui se trouve
    déjà sur le disque dans extract_dir.

    Pourquoi c'est important : extract_dir est un dossier de travail partagé
    entre toutes les images d'une même zone. S'il contient déjà d'anciens
    dossiers .SAFE issus d'extractions précédentes (résidus non nettoyés),
    un simple glob("*.SAFE") sur le disque risque de retourner un ANCIEN
    dossier au lieu de celui qu'on vient d'extraire — ce qui provoquait la
    confusion entre images de dates différentes.
    """
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            total_mb = sum(i.file_size for i in zf.infolist()) / (1024 * 1024)
            logger.info(f"Extraction de {Path(zip_path).name} (~{total_mb:.0f} Mo)...")

            # Nom du dossier .SAFE tel que déclaré DANS l'archive
            top_level_names = {name.split("/")[0] for name in zf.namelist() if name}
            safe_names = [n for n in top_level_names if n.endswith(".SAFE")]

            zf.extractall(extract_dir)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.error(f"Erreur lors de l'extraction : {exc}")
        return None

    if not safe_names:
        logger.error(
            f"Aucun dossier .SAFE déclaré dans l'archive elle-même : {zip_path}"
        )
        return None

    if len(safe_names) > 1:
        logger.warning(
            f"Plusieurs dossiers .SAFE trouvés dans l'archive ({safe_names}), "
            f"on prend le premier."
        )

    safe_dir = extract_dir / safe_names[0]
    if not safe_dir.exists():
        logger.error(
            f"Le dossier .SAFE attendu ({safe_dir}) est introuvable après extraction "
            f"— l'archive est peut-être corrompue ou incomplète."
        )
        return None

    return safe_dir


def _find_preview_png(safe_dir: Path) -> Path | None:
    """
    Cherche l'aperçu PNG fourni tel quel dans l'archive (généré par l'ESA,
    pas besoin de traitement supplémentaire). Emplacement le plus courant :
      <NOM>.SAFE/preview/quick-look.png

    Si absent à cet endroit précis, on cherche n'importe quel .png dans
    tout le dossier .SAFE en solution de secours (certains produits
    rangent l'aperçu ailleurs selon la version du format).
    """
    standard_path = safe_dir / "preview" / "quick-look.png"
    if standard_path.exists():
        return standard_path

    # Solution de secours : n'importe quel .png trouvé dans le .SAFE
    candidates = list(safe_dir.rglob("*.png"))
    if candidates:
        return candidates[0]

    return None


def _find_bands(safe_dir: Path) -> dict:
    """
    Parcourt le dossier .SAFE et retourne un dict {alias_bande: chemin_fichier}.

    Structure d'un produit Sentinel-2 L2A (CDSE) :
      <NOM>.SAFE/
        GRANULE/
          L2A_<ID>/
            IMG_DATA/
              R10m/   ← bandes à 10m : B02, B03, B04, B08
              R20m/   ← bandes à 20m : B05, B06, B07, B8A, B11, B12, SCL
              R60m/   ← bandes à 60m : B01, B09
    """
    bands = {}

    # On cherche dans tous les dossiers R*m (10m, 20m, 60m)
    for resolution_dir in safe_dir.glob("GRANULE/*/IMG_DATA/R*m"):
        for jp2_file in resolution_dir.glob("*.jp2"):
            stem = jp2_file.stem  # ex: T32SNB_20240315T094031_B04_10m
            for alias, pattern in BANDS_TO_EXTRACT.items():
                # Correspondance : le nom contient le motif de la bande
                # Priorité : on prend la résolution la plus fine si plusieurs fichiers existent
                if f"_{pattern}_" in stem or stem.endswith(f"_{pattern}"):
                    if alias not in bands:
                        bands[alias] = str(jp2_file)
                    else:
                        # Garder la résolution la plus fine (dossier R10m < R20m < R60m)
                        existing_res = _extract_resolution(bands[alias])
                        new_res = _extract_resolution(str(jp2_file))
                        if new_res < existing_res:
                            bands[alias] = str(jp2_file)

    return bands


def _extract_resolution(path: str) -> int:
    """Extrait la résolution en mètres depuis un chemin de bande (ex: R10m -> 10)."""
    match = re.search(r"R(\d+)m", path)
    return int(match.group(1)) if match else 999


def _compute_cloud_cover_from_scl(scl_path: str) -> tuple[float, float]:
    """
    Lit la bande SCL et calcule :
      - le pourcentage de pixels nuageux (parmi les pixels valides)
      - le ratio de pixels valides (non-nodata)

    Retourne (cloud_cover_percent, valid_pixel_ratio)

    NOTE : on utilise rasterio pour lire le .jp2 (JPEG2000). Rasterio s'appuie
    sur GDAL qui supporte nativement ce format. L'array retourné est 2D (hauteur,
    largeur) avec un entier entre 0 et 11 pour chaque pixel.
    """
    try:
        import rasterio  # import différé : pas nécessaire si la bande SCL est absente
    except ImportError:
        logger.error(
            "rasterio n'est pas installé. Exécutez : pip install rasterio\n"
            "La validation nuages sera ignorée pour cette image."
        )
        return 0.0, 1.0

    try:
        with rasterio.open(scl_path) as src:
            scl = src.read(1)  # lecture de la première (et unique) bande
    except Exception as exc:
        logger.error(f"Impossible de lire la bande SCL ({scl_path}) : {exc}")
        return 0.0, 1.0

    total_pixels = scl.size  # nombre total de pixels dans la tuile

    # Masque des pixels sans données (hors emprise ou défectueux)
    nodata_mask = np.isin(scl, list(SCL_NODATA_CODES))
    valid_pixels = int(total_pixels - nodata_mask.sum())

    if valid_pixels == 0:
        logger.warning("Tous les pixels sont no-data — image probablement hors zone")
        return 100.0, 0.0

    # Masque des pixels nuageux (parmi les pixels valides uniquement)
    cloud_mask = np.isin(scl, list(SCL_CLOUD_CODES))
    cloud_pixels = int((cloud_mask & ~nodata_mask).sum())

    cloud_cover_percent = float(100.0 * cloud_pixels / valid_pixels)
    valid_pixel_ratio = float(valid_pixels / total_pixels)

    logger.debug(
        f"SCL : total={total_pixels:,} | valides={valid_pixels:,} | "
        f"nuageux={cloud_pixels:,} | cloud%={cloud_cover_percent:.1f}% | "
        f"valid_ratio={valid_pixel_ratio:.2f}"
    )

    return cloud_cover_percent, valid_pixel_ratio
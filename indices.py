"""
indices.py — Calcul des indices spectraux à partir des bandes Sentinel-2 validées.

POURQUOI CES INDICES POUR LA DGRE ?
=====================================
La sous-direction de l'eau de surface a besoin de surveiller :

  1. La végétation autour des ressources en eau (bassins versants, rives)
     → NDVI, WDVI, SAVI, EVI

  2. Les plans d'eau et leur délimitation (barrages, oueds en eau, zones inondées)
     → NDWI, MNDWI (MNDWI plus robuste en zone aride/semi-aride comme la Tunisie)

  3. Le stress hydrique de la végétation (utile pour anticiper les sécheresses)
     → NDMI

  4. Une image "naturelle" directement lisible sans SIG (composite couleur RVB)
     → TRUE_COLOR (généré automatiquement si B02/B03/B04 sont disponibles)

COMMENT ÇA FONCTIONNE ?
==========================
Chaque pixel dans une image Sentinel-2 a des valeurs de réflectance pour
plusieurs longueurs d'onde (bandes). Les indices combinent ces valeurs par
des formules mathématiques simples pour mettre en évidence un phénomène.

Exemple — NDVI :
  - La végétation verte absorbe fortement le rouge (B04) et réfléchit fortement
    le proche infrarouge (B08).
  - Un sol nu ou de l'eau réfléchit à peu près pareil dans les deux bandes.
  - (NIR - RED) / (NIR + RED) donne donc un nombre proche de 1 pour la végétation
    dense, proche de 0 pour le sol nu, et négatif pour l'eau.

Normalisation des valeurs Sentinel-2
--------------------------------------
Les bandes Sentinel-2 L2A sont livrées en entiers 16 bits avec un facteur
d'échelle de 10 000. Une réflectance de surface de 0.3 (30%) est stockée
comme la valeur 3000. Avant tout calcul, on divise par 10 000 pour obtenir
des réflectances réelles entre 0 et 1.

On plafonne aussi les valeurs à [0, 1] pour éliminer les pixels aberrants
(artefacts, saturation) qui pourraient fausser les indices.

Résultats : GeoTIFF avec préservation du géoréférencement
-----------------------------------------------------------
Chaque indice est écrit en GeoTIFF Float32 dans un dossier "processed".
On préserve EXACTEMENT le CRS (système de coordonnées) et la transformation
géographique de la bande de référence, pour que les fichiers d'indices soient
directement superposables aux bandes d'origine dans QGIS ou ArcGIS.

On utilise la compression LZW (sans perte, très efficace pour les rasters
à valeurs continues) pour réduire l'espace disque.

Composite couleur (TRUE_COLOR)
--------------------------------
En plus des indices, on génère systématiquement une image "naturelle"
(rouge=B04, vert=B03, bleu=B02) dès que ces trois bandes sont disponibles —
même si aucun indice de végétation n'a été demandé. C'est cette image que
les ingénieurs/techniciens voient directement dans le chatbot sans avoir
besoin d'ouvrir QGIS.
"""

import os
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("satellite_agent.indices")

# Petit epsilon pour éviter les divisions par zéro
# (cas des pixels où les deux bandes valent exactement 0)
EPS = 1e-6

# Facteur d'échelle Sentinel-2 L2A (réflectances stockées en entiers × 10 000)
SENTINEL2_SCALE_FACTOR = 10_000.0

# Bandes nécessaires pour le composite couleur naturel (rouge, vert, bleu)
TRUE_COLOR_BANDS = ["B04", "B03", "B02"]

# Catalogue des indices : nom -> (liste de bandes requises, description)
# Permet d'afficher dynamiquement dans le dashboard quels indices sont calculables
INDEX_CATALOG = {
    "NDVI":  (["B04", "B08"],       "Normalized Difference Vegetation Index"),
    "NDWI":  (["B03", "B08"],       "Normalized Difference Water Index (McFeeters 1996) — plans d'eau"),
    "MNDWI": (["B03", "B11"],       "Modified NDWI (Xu 2006) — eau en zone aride"),
    "NDMI":  (["B08", "B11"],       "Normalized Difference Moisture Index — stress hydrique"),
    "SAVI":  (["B04", "B08"],       "Soil-Adjusted Vegetation Index — végétation sur sol nu"),
    "WDVI":  (["B04", "B08"],       "Weighted Difference Vegetation Index"),
    "EVI":   (["B02", "B04", "B08"],"Enhanced Vegetation Index — réduit la saturation en forte biomasse"),
}


# ===========================================================================
# FORMULES (fonctions pures sur arrays numpy — testables unitairement)
# ===========================================================================

def ndvi(red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """
    Normalized Difference Vegetation Index
    Formule : (NIR - RED) / (NIR + RED)
    Plage théorique : -1 à +1
      > 0.5  = végétation dense
      0.2-0.5 = végétation modérée
      0-0.2  = sol nu / végétation très éparse
      < 0    = eau, neige, nuages
    """
    return (nir - red) / (nir + red + EPS)


def ndwi(green: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """
    Normalized Difference Water Index (McFeeters, 1996)
    Formule : (GREEN - NIR) / (GREEN + NIR)
    Plage : -1 à +1
      > 0    = surface en eau (l'eau absorbe le NIR, réfléchit le vert)
      < 0    = végétation, sol sec
    Utile pour : délimitation des plans d'eau, suivi des niveaux des barrages
    """
    return (green - nir) / (green + nir + EPS)


def mndwi(green: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """
    Modified Normalized Difference Water Index (Xu, 2006)
    Formule : (GREEN - SWIR1) / (GREEN + SWIR1)
    Plus robuste que le NDWI en milieu aride/urbain :
    - Réduit la confusion avec les zones bâties
    - Distingue mieux l'eau des terrains sableux (sols tunisiens)
    Recommandé pour la Tunisie.
    """
    return (green - swir1) / (green + swir1 + EPS)


def ndmi(nir: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """
    Normalized Difference Moisture Index
    Formule : (NIR - SWIR1) / (NIR + SWIR1)
    Plage : -1 à +1
      > 0.4  = végétation bien hydratée
      0-0.4  = végétation sous stress hydrique
      < 0    = sol très sec ou eau
    Utile pour : anticiper les sécheresses, évaluer l'état des cultures irriguées
    """
    return (nir - swir1) / (nir + swir1 + EPS)


def savi(red: np.ndarray, nir: np.ndarray, L: float = 0.5) -> np.ndarray:
    """
    Soil-Adjusted Vegetation Index (Huete, 1988)
    Formule : ((NIR - RED) / (NIR + RED + L)) × (1 + L)
    L = facteur de correction du sol (0.5 par défaut, recommandé pour
    une densité de végétation intermédiaire — pertinent pour la Tunisie)
    Avantage vs NDVI : atténue l'influence des sols nus visibles à travers
    la canopée — important dans les zones semi-arides.
    """
    return ((nir - red) / (nir + red + L + EPS)) * (1.0 + L)


def wdvi(red: np.ndarray, nir: np.ndarray, g: float = 1.0) -> np.ndarray:
    """
    Weighted Difference Vegetation Index (Clevers, 1989)
    Formule : NIR - g × RED
    g = pente de la "soil line" (droite reliant les pixels de sol nu dans
    l'espace rouge/NIR). Par défaut g=1.0 (isotrope). Pour une calibration
    locale précise, des relevés de terrain (spectroradiomètre sur sol tunisien)
    permettraient d'affiner ce paramètre.
    Pas normalisé (-inf à +inf) : les valeurs absolues n'ont de sens que
    comparées entre images de la même série temporelle.
    """
    return nir - g * red


def evi(blue: np.ndarray, red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """
    Enhanced Vegetation Index (Huete et al., 2002)
    Formule : 2.5 × (NIR - RED) / (NIR + 6×RED - 7.5×BLUE + 1)
    Avantages vs NDVI :
    - Ne sature pas en végétation très dense (forêt)
    - Réduit les effets atmosphériques résiduels via la bande bleue
    Moins pertinent que NDVI pour la Tunisie (végétation éparse),
    mais utile pour les zones agricoles irriguées de la plaine de la Medjerda.
    """
    return 2.5 * (nir - red) / (nir + 6.0 * red - 7.5 * blue + 1.0 + EPS)


# ===========================================================================
# FONCTION PRINCIPALE
# ===========================================================================

def compute_indices(bands: dict, output_dir: str,
                    requested: list = None) -> dict:
    """
    Calcule les indices spectraux demandés et les écrit en GeoTIFF.
    Génère en plus, systématiquement, un composite couleur naturel
    (TRUE_COLOR.tif) dès que B02/B03/B04 sont disponibles — c'est l'image
    "brute" que les utilisateurs peuvent voir sans SIG.

    Paramètres
    ----------
    bands : dict
        Chemins des bandes extraites par validator.py, ex:
        {"B02": "/path/B02.jp2", "B03": "...", "B04": "...", "B08": "...", ...}
    output_dir : str
        Dossier où écrire les GeoTIFF résultats (sera créé si inexistant)
    requested : list | None
        Liste des indices à calculer, ex: ["NDVI", "NDWI", "MNDWI"].
        Si None, calcule tous les indices possibles selon les bandes disponibles.

    Retourne
    --------
    dict {nom_indice: {"path": chemin_geotiff, "stats": {min,max,mean,std}}}
    Les indices effectivement calculés, plus "TRUE_COLOR" si généré.
    """
    try:
        import rasterio
        from rasterio.transform import from_bounds
        from rasterio.enums import Resampling
    except ImportError:
        logger.error("rasterio non installé. Exécutez : pip install rasterio")
        return {}

    os.makedirs(output_dir, exist_ok=True)

    # Déterminer quels indices sont calculables avec les bandes disponibles
    if requested is None:
        requested = list(INDEX_CATALOG.keys())

    calculable = []
    for name in requested:
        required_bands, _ = INDEX_CATALOG[name]
        missing = [b for b in required_bands if b not in bands]
        if missing:
            logger.warning(f"Indice {name} ignoré — bandes manquantes : {missing}")
        else:
            calculable.append(name)

    if not calculable:
        logger.warning("Aucun indice calculable avec les bandes disponibles.")

    # Chargement des bandes nécessaires (une seule fois chacune)
    # On charge aussi B02/B03/B04 si présentes, pour le composite couleur,
    # même si aucun indice demandé n'en a besoin.
    needed_bands = set()
    for name in calculable:
        required, _ = INDEX_CATALOG[name]
        needed_bands.update(required)

    can_make_true_color = all(b in bands for b in TRUE_COLOR_BANDS)
    if can_make_true_color:
        needed_bands.update(TRUE_COLOR_BANDS)

    if not needed_bands:
        return {}
    # Sentinel-2 mélange des bandes à 10m (B02,B03,B04,B08) et à 20m
    # (B11, B12, B8A...). Combiner directement des arrays de tailles
    # différentes fait planter numpy ("operands could not be broadcast").
    # On choisit toujours une bande native 10m comme référence géométrique,
    # et on rééchantillonne (bilinéaire) toute bande 20m vers cette même
    # grille avant tout calcul, pour que toutes les bandes chargées aient
    # exactement la même forme.
    NATIVE_10M_ORDER = ["B04", "B08", "B03", "B02"]
    reference_alias = next((b for b in NATIVE_10M_ORDER if b in needed_bands), None)
    if reference_alias is None:
        reference_alias = next(iter(needed_bands))

    loaded = {}
    profile = None
    ref_shape = None

    # 1) Charger la bande de référence en premier, pour fixer la géométrie cible
    ref_path = bands[reference_alias]
    try:
        with rasterio.open(ref_path) as src:
            arr = src.read(1).astype("float32")
            arr = np.clip(arr / SENTINEL2_SCALE_FACTOR, 0.0, 1.0)
            loaded[reference_alias] = arr
            profile = src.profile.copy()
            ref_shape = (src.height, src.width)
            logger.debug(
                f"Bande de référence (10m) : {reference_alias} | "
                f"CRS={profile.get('crs')}, taille={src.width}×{src.height}"
            )
    except Exception as exc:
        logger.error(f"Impossible de lire la bande de référence {reference_alias} ({ref_path}) : {exc}")
        return {}

    # 2) Charger les autres bandes, en les rééchantillonnant si besoin
    for band_alias in needed_bands:
        if band_alias == reference_alias:
            continue
        path = bands[band_alias]
        try:
            with rasterio.open(path) as src:
                if (src.height, src.width) != ref_shape:
                    arr = src.read(
                        1,
                        out_shape=(1, ref_shape[0], ref_shape[1]),
                        resampling=Resampling.bilinear,
                    ).astype("float32")
                    logger.debug(
                        f"Bande {band_alias} rééchantillonnée "
                        f"{src.height}×{src.width} → {ref_shape[0]}×{ref_shape[1]}"
                    )
                else:
                    arr = src.read(1).astype("float32")
                arr = np.clip(arr / SENTINEL2_SCALE_FACTOR, 0.0, 1.0)
                loaded[band_alias] = arr
        except Exception as exc:
            logger.error(f"Impossible de lire la bande {band_alias} ({path}) : {exc}")
            return {}
        
    # Calcul de chaque indice
    results = {}
    for name in calculable:
        try:
            index_array = _compute_single_index(name, loaded)
            if index_array is None:
                continue

            # Écriture en GeoTIFF (Float32, compression LZW)
            out_path = os.path.join(output_dir, f"{name}.tif")
            _write_geotiff(index_array, out_path, profile)

            # Statistiques descriptives (utiles pour le dashboard)
            valid = index_array[np.isfinite(index_array)]
            stats = {}
            if valid.size > 0:
                stats = {
                    "min":  float(np.min(valid)),
                    "max":  float(np.max(valid)),
                    "mean": float(np.mean(valid)),
                    "std":  float(np.std(valid)),
                }

            results[name] = {"path": out_path, "stats": stats}

            if stats:
                logger.info(
                    f"✅ {name} → {out_path} | "
                    f"min={stats['min']:.3f} max={stats['max']:.3f} mean={stats['mean']:.3f}"
                )
            else:
                logger.info(f"✅ {name} → {out_path} | aucun pixel valide pour les statistiques")

        except Exception as exc:
            logger.error(f"Erreur lors du calcul de {name} : {exc}")

    # Composite couleur naturel (TRUE_COLOR) — image "brute" sans SIG
    if can_make_true_color:
        try:
            out_path = os.path.join(output_dir, "TRUE_COLOR.tif")
            _write_true_color_geotiff(
                red=loaded["B04"], green=loaded["B03"], blue=loaded["B02"],
                out_path=out_path, reference_profile=profile,
            )
            results["TRUE_COLOR"] = {"path": out_path, "stats": {}}
            logger.info(f"✅ TRUE_COLOR → {out_path}")
        except Exception as exc:
            logger.error(f"Erreur lors de la génération du composite couleur : {exc}")

    return results


# ===========================================================================
# FONCTIONS INTERNES
# ===========================================================================

def _compute_single_index(name: str, loaded: dict) -> np.ndarray | None:
    """Applique la formule d'un indice aux arrays déjà chargés."""
    # Récupération des bandes avec alias harmonisés
    b = loaded  # raccourci

    if name == "NDVI":
        return ndvi(red=b["B04"], nir=b["B08"])
    elif name == "NDWI":
        return ndwi(green=b["B03"], nir=b["B08"])
    elif name == "MNDWI":
        # B11 (20m) est rééchantillonnée à 10m en amont, dans compute_indices()
        # — les deux arrays ont donc la même forme ici.
        return mndwi(green=b["B03"], swir1=b["B11"])
    elif name == "NDMI":
        return ndmi(nir=b["B08"], swir1=b["B11"])
    elif name == "SAVI":
        return savi(red=b["B04"], nir=b["B08"])
    elif name == "WDVI":
        return wdvi(red=b["B04"], nir=b["B08"])
    elif name == "EVI":
        return evi(blue=b["B02"], red=b["B04"], nir=b["B08"])
    else:
        logger.warning(f"Indice inconnu : {name}")
        return None


def _write_geotiff(array: np.ndarray, out_path: str, reference_profile: dict):
    """
    Écrit un array numpy en GeoTIFF Float32 en préservant le géoréférencement
    de la bande de référence (CRS + transformation affine).

    Le format GeoTIFF est lisible directement par QGIS, ArcGIS, GDAL, rasterio,
    et la plupart des outils SIG. La compression LZW réduit la taille du fichier
    d'environ 50% sans perte de données.
    """
    import rasterio

    profile = reference_profile.copy()
    profile.update({
        "dtype":   "float32",
        "count":   1,           # une seule bande (l'indice)
        "compress": "lzw",
        "nodata":  float("nan"),
        "driver":  "GTiff",
    })

    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(array.astype("float32"), 1)


def _write_true_color_geotiff(red: np.ndarray, green: np.ndarray, blue: np.ndarray,
                               out_path: str, reference_profile: dict):
    """
    Écrit un composite couleur naturel (R=B04, V=B03, B=B02) en GeoTIFF 3 bandes,
    en réflectances [0, 1] Float32 — géoréférencé comme les indices.

    Reste en Float32 (pas d'étirement de contraste ici) pour rester exploitable
    en SIG comme les autres GeoTIFF. L'étirement de contraste pour un affichage
    "joli" est fait uniquement à l'affichage (côté chatbot/dashboard), pas ici.
    """
    import rasterio

    profile = reference_profile.copy()
    profile.update({
        "dtype":    "float32",
        "count":    3,          # 3 bandes : rouge, vert, bleu
        "compress": "lzw",
        "nodata":   float("nan"),
        "driver":   "GTiff",
    })

    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(red.astype("float32"), 1)
        dst.write(green.astype("float32"), 2)
        dst.write(blue.astype("float32"), 3)

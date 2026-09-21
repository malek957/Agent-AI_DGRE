"""
calc_indices.py v2 — Calcule les indices et génère les PNG.
Correction : rééchantillonnage des bandes 20m → 10m pour MNDWI et NDMI.
"""
import sys
import os
import numpy as np
import rasterio
from rasterio.enums import Resampling
from pathlib import Path
from matplotlib import pyplot as plt

sys.path.insert(0, '.')
from validator import _find_bands

# ── Image Sentinel-2 disponible ──────────────────────────────────────────────
SAFE_DIR = Path(
    "data/tmp/SENTINEL-2/tunis_sentinel2/extracted/"
    "S2C_MSIL2A_20260904T100551_N0512_R022_T32SPF_20260904T154108.SAFE"
)
OUTPUT_DIR = "data/processed/SENTINEL-2/tunis_sentinel2/2026/09/S2C_20260904"
SCALE = 10_000.0  # facteur d'échelle Sentinel-2 L2A

print("=" * 60)
print("CALCUL DES INDICES — Sentinel-2 | Tunis | 04/09/2026")
print("=" * 60)

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── 1. Trouver les bandes ────────────────────────────────────────────────────
print("\n1. Recherche des bandes...")
bands = _find_bands(SAFE_DIR)
print(f"   Bandes trouvées : {list(bands.keys())}")


# ── 2. Lire une bande avec rééchantillonnage optionnel ───────────────────────
def read_band(alias: str, target_shape: tuple = None) -> np.ndarray:
    """
    Lit une bande et la rééchantillonne à target_shape si nécessaire.
    Cela permet de combiner des bandes à 10m et 20m dans le même calcul.
    """
    path = bands.get(alias)
    if not path:
        raise ValueError(f"Bande {alias} introuvable")

    with rasterio.open(path) as src:
        if target_shape and (src.height, src.width) != target_shape:
            # Rééchantillonnage bilinéaire vers la résolution cible
            data = src.read(
                1,
                out_shape=(1, target_shape[0], target_shape[1]),
                resampling=Resampling.bilinear,
            ).astype("float32")
        else:
            data = src.read(1).astype("float32")
            target_shape = (src.height, src.width)

    # Normalisation [0, 1]
    data = np.clip(data / SCALE, 0.0, 1.0)
    return data, target_shape


EPS = 1e-6


# ── 3. Calculer les indices ───────────────────────────────────────────────────
print("\n2. Calcul des indices...")

results = {}

# Lire les bandes à 10m (référence)
red,   shape = read_band("B04")
nir,   _     = read_band("B08", shape)
green, _     = read_band("B03", shape)
blue,  _     = read_band("B02", shape)

# Bandes à 20m rééchantillonnées à 10m
swir1, _     = read_band("B11", shape)  # ← rééchantillonné 20m→10m

# NDVI
ndvi = (nir - red) / (nir + red + EPS)
results["NDVI"]  = {"data": ndvi,  "range": (-0.5, 0.8), "cmap": "RdYlGn",
                    "legend": "Vert=végétation dense | Rouge=sol nu | Bleu=eau"}

# NDWI
ndwi = (green - nir) / (green + nir + EPS)
results["NDWI"]  = {"data": ndwi,  "range": (-0.5, 0.5), "cmap": "Blues_r",
                    "legend": "Bleu foncé=eau libre | Blanc=sec"}

# MNDWI (corrigé — B11 rééchantillonné)
mndwi = (green - swir1) / (green + swir1 + EPS)
results["MNDWI"] = {"data": mndwi, "range": (-0.5, 0.5), "cmap": "Blues_r",
                    "legend": "Bleu=eau zone aride | Blanc=sec (recommandé Tunisie)"}

# NDMI (corrigé — B11 rééchantillonné)
ndmi = (nir - swir1) / (nir + swir1 + EPS)
results["NDMI"]  = {"data": ndmi,  "range": (-0.5, 0.5), "cmap": "RdYlBu",
                    "legend": "Bleu=végétation hydratée | Rouge=stress hydrique"}

# SAVI
L = 0.5
savi = ((nir - red) / (nir + red + L + EPS)) * (1.0 + L)
results["SAVI"]  = {"data": savi,  "range": (-0.5, 0.8), "cmap": "RdYlGn",
                    "legend": "Vert=végétation | Rouge=sol nu (corrigé sol)"}

# WDVI
wdvi = nir - 1.0 * red
results["WDVI"]  = {"data": wdvi,  "range": (-0.3, 0.5), "cmap": "RdYlGn",
                    "legend": "Vert=végétation | Rouge=sol nu (pondéré)"}

print(f"   {len(results)} indices calculés : {list(results.keys())}")


# ── 4. Lire le profil géospatial pour écrire les GeoTIFF ─────────────────────
with rasterio.open(bands["B04"]) as src:
    profile = src.profile.copy()
    profile.update(dtype="float32", count=1, compress="lzw",
                   nodata=float("nan"), driver="GTiff")


# ── 5. Écrire GeoTIFF + PNG ───────────────────────────────────────────────────
print("\n3. Génération des GeoTIFF et PNG...")

generated_png = []

for name, info in results.items():
    data    = info["data"].astype("float32")
    tif_path = os.path.join(OUTPUT_DIR, f"{name}.tif")
    png_path = os.path.join(OUTPUT_DIR, f"{name}.png")

    # Écrire GeoTIFF
    with rasterio.open(tif_path, "w", **profile) as dst:
        dst.write(data, 1)

    # Statistiques
    valid = data[np.isfinite(data)]
    stats = {
        "min":  float(np.nanmin(valid)),
        "max":  float(np.nanmax(valid)),
        "mean": float(np.nanmean(valid)),
        "std":  float(np.nanstd(valid)),
    }

    # Générer PNG coloré
    vmin, vmax = info["range"]
    fig, ax = plt.subplots(figsize=(12, 10))

    im = ax.imshow(data, cmap=info["cmap"], vmin=vmin, vmax=vmax,
                   interpolation="nearest")

    cbar = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label(f"Valeur {name}", fontsize=11)

    ax.set_title(
        f"{name} — Sentinel-2 (S2C) | Zone : Tunis | Date : 04/09/2026\n"
        f"min={stats['min']:.3f}  max={stats['max']:.3f}  "
        f"mean={stats['mean']:.3f}  std={stats['std']:.3f}",
        fontsize=13, fontweight="bold", pad=15,
    )
    ax.axis("off")

    # Légende en bas
    ax.text(
        0.5, -0.03, info["legend"],
        transform=ax.transAxes,
        ha="center", fontsize=10,
        style="italic", color="dimgray",
    )

    # Watermark DGRE
    ax.text(
        0.99, 0.01,
        "DGRE — Sous-direction de l'eau de surface",
        transform=ax.transAxes,
        ha="right", fontsize=8, color="gray", alpha=0.7,
    )

    plt.tight_layout()
    plt.savefig(png_path, dpi=150, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close()

    generated_png.append(png_path)
    print(
        f"   ✅ {name:6} → {png_path}\n"
        f"          min={stats['min']:.3f} max={stats['max']:.3f} "
        f"mean={stats['mean']:.3f}"
    )


# ── 6. Résumé ─────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("✅ TERMINÉ — PNG générés :")
for f in generated_png:
    print(f"   📄 {f}")
print(f"\nDossier : {os.path.abspath(OUTPUT_DIR)}")
print("=" * 60)
print("\nOuvrez les PNG dans l'explorateur Windows pour visualiser.")
print("Ces fichiers peuvent être téléchargés depuis le dashboard.")
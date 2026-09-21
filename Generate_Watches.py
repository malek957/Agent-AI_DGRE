"""
Générateur automatique de WATCHES à partir de TOUS les shapefiles
présents dans data/shapefiles/.

Prérequis :
    pip install geopandas shapely pyproj

Utilisation :
    python generate_watches.py
"""

import glob
import os
import unicodedata
import re

import geopandas as gpd

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

SHAPEFILES_DIR = "data/shapefiles/fwdannuaire"

COLLECTION = "SENTINEL-2"
PRODUCT_TYPE = "S2MSI2A"
MAX_CLOUD_COVER = 60
CLOUD_THRESHOLD = 20

OUTPUT_FILE = "watches_config.py"

# Marge de sécurité (en degrés) autour de chaque bbox
BUFFER_DEG = 0.01


def slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


def guess_name_field(gdf) -> str:
    """
    Essaie de deviner automatiquement quelle colonne contient le nom
    de la zone, en cherchant des noms de colonnes courants.
    Si rien n'est trouvé, utilise la première colonne de type texte.
    """
    candidates = [
    "LIB_FR", "LIB_AR",
    "NOM_GOUV", "NOM_DELEG", "NOM_BASSIN", "NOM",
    "NAME", "NAME_1", "NAME_2", "GOUV_NAME", "DELEG_NAME",
    "nom", "name",
    ]
    for col in candidates:
        if col in gdf.columns:
            return col

    # Sinon, on prend la première colonne texte (hors geometry)
    for col in gdf.columns:
        if col != "geometry" and gdf[col].dtype == object:
            return col

    raise ValueError(
        f"Impossible de deviner la colonne du nom. Colonnes disponibles : "
        f"{list(gdf.columns)}"
    )


def process_shapefile(shp_path: str):
    """Lit un shapefile et retourne la liste des WATCHES correspondants."""
    filename = os.path.basename(shp_path)
    prefix = slugify(os.path.splitext(filename)[0])

    gdf = gpd.read_file(shp_path)

    if gdf.crs is None:
        print(f"⚠️  {filename} : pas de CRS défini, ignoré.")
        return []

    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    name_field = guess_name_field(gdf)
    print(f"   → colonne utilisée pour le nom : '{name_field}'")

    watches = []
    for _, row in gdf.iterrows():
        raw_name = row[name_field]
        if raw_name is None:
            continue

        minx, miny, maxx, maxy = row.geometry.bounds
        bbox = [
            round(minx - BUFFER_DEG, 4),
            round(miny - BUFFER_DEG, 4),
            round(maxx + BUFFER_DEG, 4),
            round(maxy + BUFFER_DEG, 4),
        ]

        watches.append(
            {
                "name": f"{prefix}_{slugify(str(raw_name))}",
                "collection": COLLECTION,
                "product_type": PRODUCT_TYPE,
                "bbox": bbox,
                "max_cloud_cover": MAX_CLOUD_COVER,
                "cloud_threshold": CLOUD_THRESHOLD,
            }
        )

    print(f"✅ {filename} : {len(watches)} zones traitées")
    return watches


def generate_all_watches():
    shp_files = sorted(glob.glob(os.path.join(SHAPEFILES_DIR, "*.shp")))

    if not shp_files:
        raise FileNotFoundError(
            f"Aucun fichier .shp trouvé dans {SHAPEFILES_DIR}. "
            f"Vérifiez que vos fichiers sont bien à cet endroit."
        )

    all_watches = []
    for shp_path in shp_files:
        print(f"Traitement de {shp_path} ...")
        all_watches.extend(process_shapefile(shp_path))

    return all_watches


def write_config(watches, path=OUTPUT_FILE):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Généré automatiquement — ne pas éditer à la main\n")
        f.write("WATCHES = [\n")
        for w in watches:
            f.write("    {\n")
            f.write(f'        "name": "{w["name"]}",\n')
            f.write(f'        "collection": "{w["collection"]}",\n')
            f.write(f'        "product_type": "{w["product_type"]}",\n')
            f.write(f'        "bbox": {w["bbox"]},\n')
            f.write(f'        "max_cloud_cover": {w["max_cloud_cover"]},\n')
            f.write(f'        "cloud_threshold": {w["cloud_threshold"]},\n')
            f.write("    },\n")
        f.write("]\n")

    print(f"\n📄 Total : {len(watches)} zones écrites dans {path}")


if __name__ == "__main__":
    watches = generate_all_watches()
    write_config(watches)
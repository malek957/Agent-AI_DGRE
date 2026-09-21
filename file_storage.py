"""
storage.py — Gestion de l'organisation du stockage des images.

PROBLÈME : où stocker les milliers d'images téléchargées ?
=============================================================
Sans organisation, le dossier data/ devient rapidement un bazar :
des centaines de .zip mélangés avec des .SAFE extraits, des GeoTIFF
d'indices, des logs... Impossible de s'y retrouver.

Notre convention d'arborescence
---------------------------------
Toutes les images sont organisées selon une hiérarchie logique :

  <STORAGE_ROOT>/
  ├── raw/
  │   └── SENTINEL-2/
  │       └── tunis_sentinel2/
  │           └── 2026/
  │               └── 03/
  │                   └── S2A_MSIL2A_20260315_....SAFE/  ← dossier .SAFE extrait
  │
  ├── processed/
  │   └── SENTINEL-2/
  │       └── tunis_sentinel2/
  │           └── 2026/
  │               └── 03/
  │                   └── S2A_MSIL2A_20260315_.../
  │                       ├── NDVI.tif
  │                       ├── NDWI.tif
  │                       ├── MNDWI.tif
  │                       └── ...
  │
  └── quarantine/           ← scènes rejetées (conservées pour audit)
      └── SENTINEL-2/
          └── tunis_sentinel2/
              └── 2026/
                  └── 03/
                      └── S2A_MSIL2A_20260315_....SAFE/

Pourquoi conserver les images rejetées (quarantaine) ?
-------------------------------------------------------
Si un utilisateur demande "pourquoi n'y a-t-il pas d'image le 15 mars ?",
on peut lui répondre "il y en avait une mais elle était trop nuageuse (62%)".
Sans la quarantaine, cette information serait perdue.

Pourquoi la séparation raw / processed ?
-----------------------------------------
- raw = bandes brutes téléchargées (lourdes, ~700 Mo par scène) →
  peuvent être supprimées une fois les indices calculés pour économiser l'espace
- processed = indices GeoTIFF légers (~50-100 Mo par scène) →
  à conserver pour le long terme

Bascule NAS (partage réseau DGRE)
-----------------------------------
La variable STORAGE_ROOT dans .env permet de pointer vers n'importe quel
chemin, y compris un partage réseau monté :
  - Simulation locale : STORAGE_ROOT=./data
  - NAS DGRE (Linux)  : STORAGE_ROOT=/mnt/dgre-nas
  - NAS DGRE (Windows): STORAGE_ROOT=Z:\\DGRE\\Satellites

Aucune modification de code nécessaire, uniquement .env.
"""

import os
import shutil
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("satellite_agent.storage")


class StorageManager:
    """
    Gère l'arborescence de stockage des images satellitaires.
    Toutes les opérations sur les chemins passent par cette classe,
    ce qui garantit une organisation cohérente quelle que soit la
    racine configurée (locale ou réseau partagé).
    """

    def __init__(self, storage_root: str):
        self.root = Path(storage_root)
        self._ensure_structure()

    def _ensure_structure(self):
        """Crée l'arborescence de base si elle n'existe pas encore."""
        for subdir in ("raw", "processed", "quarantine", "logs"):
            (self.root / subdir).mkdir(parents=True, exist_ok=True)
        logger.debug(f"Stockage initialisé à la racine : {self.root.resolve()}")

    def _scene_subpath(self, collection: str, watch_name: str,
                       acquisition_date: str) -> Path:
        """
        Construit le sous-chemin <collection>/<watch>/<AAAA>/<MM>/.
        La date d'acquisition est au format ISO 8601 (ex: 2026-03-15T09:40:31.000Z).
        """
        try:
            dt = datetime.fromisoformat(acquisition_date.replace("Z", "+00:00"))
            year = f"{dt.year:04d}"
            month = f"{dt.month:02d}"
        except (ValueError, AttributeError):
            year, month = "unknown", "unknown"

        return Path(collection) / watch_name / year / month

    def get_raw_dir(self, collection: str, watch_name: str,
                    product_name: str, acquisition_date: str) -> Path:
        """Retourne (et crée) le dossier de destination des bandes brutes."""
        subpath = self._scene_subpath(collection, watch_name, acquisition_date)
        target = self.root / "raw" / subpath / product_name
        target.mkdir(parents=True, exist_ok=True)
        return target

    def get_processed_dir(self, collection: str, watch_name: str,
                           product_name: str, acquisition_date: str) -> Path:
        """Retourne (et crée) le dossier pour les indices calculés."""
        subpath = self._scene_subpath(collection, watch_name, acquisition_date)
        target = self.root / "processed" / subpath / product_name
        target.mkdir(parents=True, exist_ok=True)
        return target

    def get_quarantine_dir(self, collection: str, watch_name: str,
                            product_name: str, acquisition_date: str) -> Path:
        """Retourne (et crée) le dossier quarantaine pour les scènes rejetées."""
        subpath = self._scene_subpath(collection, watch_name, acquisition_date)
        target = self.root / "quarantine" / subpath / product_name
        target.mkdir(parents=True, exist_ok=True)
        return target

    def move_extracted_to_raw(self, safe_dir: str, collection: str,
                               watch_name: str, product_name: str,
                               acquisition_date: str) -> str:
        """
        Déplace le dossier .SAFE extrait vers l'arborescence raw/ organisée.
        Retourne le nouveau chemin.
        """
        dest = self.get_raw_dir(collection, watch_name, product_name, acquisition_date)
        safe_path = Path(safe_dir)
        dest_safe = dest / safe_path.name

        if dest_safe.exists():
            logger.info(f"Dossier .SAFE déjà présent dans raw/ : {dest_safe}")
            return str(dest_safe)

        shutil.move(str(safe_path), str(dest_safe))
        logger.info(f"Déplacé vers raw/ : {dest_safe}")
        return str(dest_safe)

    def move_to_quarantine(self, safe_dir: str, collection: str,
                            watch_name: str, product_name: str,
                            acquisition_date: str) -> str:
        """
        Déplace une scène rejetée vers la quarantaine.
        Conserve les données pour audit (traçabilité).
        """
        dest = self.get_quarantine_dir(collection, watch_name, product_name, acquisition_date)
        safe_path = Path(safe_dir)
        dest_safe = dest / safe_path.name

        if dest_safe.exists():
            logger.info(f"Déjà en quarantaine : {dest_safe}")
            return str(dest_safe)

        shutil.move(str(safe_path), str(dest_safe))
        logger.warning(f"Image rejetée → quarantaine : {dest_safe}")
        return str(dest_safe)

    def cleanup_zip(self, zip_path: str):
        """
        Supprime le fichier .zip source après extraction réussie.
        Économise l'espace disque (les bandes extraites + indices sont
        plus utiles que le zip brut).
        """
        try:
            os.remove(zip_path)
            logger.debug(f"Archive .zip supprimée après extraction : {zip_path}")
        except OSError as exc:
            logger.warning(f"Impossible de supprimer le .zip ({zip_path}) : {exc}")

    def get_usage_summary(self) -> dict:
        """
        Calcule l'espace disque utilisé par catégorie.
        Affiché dans le dashboard Streamlit.
        """
        summary = {}
        for category in ("raw", "processed", "quarantine"):
            cat_path = self.root / category
            if not cat_path.exists():
                summary[category] = {"size_mb": 0, "file_count": 0}
                continue
            total_size = sum(
                f.stat().st_size
                for f in cat_path.rglob("*")
                if f.is_file()
            )
            file_count = sum(1 for f in cat_path.rglob("*") if f.is_file())
            summary[category] = {
                "size_mb": round(total_size / (1024 * 1024), 1),
                "file_count": file_count,
                "path": str(cat_path),
            }
        return summary

    def is_writable(self) -> tuple[bool, str]:
        """
        Vérifie que le stockage est accessible en écriture.
        Utile pour le healthcheck du dashboard.
        """
        test_file = self.root / ".write_test"
        try:
            test_file.write_text("ok")
            test_file.unlink()
            return True, f"Stockage accessible : {self.root.resolve()}"
        except OSError as exc:
            return False, f"Stockage inaccessible : {exc}"

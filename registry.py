"""
registry.py — Registre local SQLite étendu pour tracer le pipeline complet.

ÉVOLUTION PAR RAPPORT À LA VERSION 1
=======================================
La v1 avait une table simple avec juste "téléchargé ou pas".
La v2 trace chaque étape du pipeline, les statistiques qualité, et
les chemins vers les résultats — informations directement lues par
le dashboard Streamlit pour construire les graphiques et les KPIs.

Colonnes ajoutées :
  - status              : étape courante dans le pipeline
  - rejection_reason    : explication textuelle si rejetée
  - cloud_cover_scl     : % nuages recalculé localement sur la SCL
  - valid_pixel_ratio   : proportion de pixels valides
  - raw_dir             : chemin vers le .SAFE extrait
  - processed_dir       : chemin vers les GeoTIFF d'indices
  - indices_computed    : liste des indices calculés (ex: "NDVI,NDWI,MNDWI")
"""

import sqlite3
import logging
from contextlib import contextmanager

logger = logging.getLogger("satellite_agent.registry")

# Schéma étendu de la base de données
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloaded_products (
    product_id          TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    collection          TEXT NOT NULL,
    watch_name          TEXT NOT NULL,
    content_date        TEXT,
    downloaded_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    file_path           TEXT,
    status              TEXT DEFAULT 'downloaded',
    rejection_reason    TEXT,
    cloud_cover_provider REAL,
    cloud_cover_scl     REAL,
    valid_pixel_ratio   REAL,
    raw_dir             TEXT,
    processed_dir       TEXT,
    indices_computed    TEXT,
    footprint_geojson   TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at     TEXT,
    triggered_by    TEXT DEFAULT 'agent',
    scenes_found    INTEGER DEFAULT 0,
    downloaded      INTEGER DEFAULT 0,
    validated       INTEGER DEFAULT 0,
    rejected        INTEGER DEFAULT 0,
    indices_done    INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running',
    notes           TEXT
);
"""


class Registry:
    def __init__(self, db_path: str = "registry.sqlite3"):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row   # permet d'accéder aux colonnes par nom
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(DB_SCHEMA)
            conn.commit()
        # Migration : ajout des nouvelles colonnes si la base existante est en v1
        self._migrate_v1_to_v2()

    def _migrate_v1_to_v2(self):
        """
        Ajoute les nouvelles colonnes à une base existante (v1 → v2).
        SQLite ne supporte pas ALTER TABLE ADD COLUMN sur plusieurs colonnes
        en une seule commande, donc on les ajoute une par une avec IF NOT EXISTS
        simulé via une exception ignorée.
        """
        new_columns = [
            ("rejection_reason",    "TEXT"),
            ("cloud_cover_provider", "REAL"),
            ("cloud_cover_scl",     "REAL"),
            ("valid_pixel_ratio",   "REAL"),
            ("raw_dir",             "TEXT"),
            ("processed_dir",       "TEXT"),
            ("indices_computed",    "TEXT"),
            ("updated_at",          "TEXT"),
            ("footprint_geojson",   "TEXT"),
        ]
        with self._connect() as conn:
            for col_name, col_type in new_columns:
                try:
                    conn.execute(
                        f"ALTER TABLE downloaded_products ADD COLUMN {col_name} {col_type}"
                    )
                    conn.commit()
                    logger.info(f"Migration v1→v2 : colonne '{col_name}' ajoutée")
                except sqlite3.OperationalError:
                    pass  # colonne déjà présente, on ignore

    # ------------------------------------------------------------------
    # Vérifications (anti-doublon)
    # ------------------------------------------------------------------

    def already_downloaded(self, product_id: str) -> bool:
        """Retourne True si le produit a déjà été téléchargé (même si rejeté ensuite)."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM downloaded_products WHERE product_id = ?",
                (product_id,)
            )
            return cur.fetchone() is not None

    def already_processed(self, product_id: str) -> bool:
        """
        Retourne True si le produit a déjà été traité complètement
        (archivé, validé, ou rejeté — tout sauf 'failed' et 'downloaded').
        Les produits 'failed' ou 'downloaded' (incomplets) seront retentés.
        """
        with self._connect() as conn:
            cur = conn.execute(
                """SELECT status FROM downloaded_products
                   WHERE product_id = ?""",
                (product_id,)
            )
            row = cur.fetchone()
            if not row:
                return False
            # On retente les échecs et les téléchargements incomplets
            return row["status"] not in ("failed", "downloaded")

    # ------------------------------------------------------------------
    # Écritures
    # ------------------------------------------------------------------

    def mark_downloaded(self, product_id: str, name: str, collection: str,
                         watch_name: str, content_date: str, file_path: str,
                         status: str = "downloaded", cloud_cover_provider: float = None,
                         footprint_geojson: str = None):
        """Insère ou remplace l'entrée d'un produit après téléchargement."""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO downloaded_products
                   (product_id, name, collection, watch_name, content_date,
                    file_path, status, cloud_cover_provider, footprint_geojson, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (product_id, name, collection, watch_name, content_date,
                 file_path, status, cloud_cover_provider, footprint_geojson)
            )
            conn.commit()

    def update_status(self, product_id: str, status: str, **kwargs):
        """
        Met à jour le statut et les métadonnées d'un produit après une étape du pipeline.

        Kwargs acceptés :
          reason             (str)   : raison du rejet
          cloud_cover_scl    (float) : % nuages recalculé
          valid_pixel_ratio  (float) : ratio pixels valides
          raw_dir            (str)   : chemin .SAFE extrait
          processed_dir      (str)   : chemin dossier indices
          indices_computed   (str)   : "NDVI,NDWI,MNDWI"
        """
        allowed = {
            "reason":             "rejection_reason",
            "cloud_cover_scl":    "cloud_cover_scl",
            "valid_pixel_ratio":  "valid_pixel_ratio",
            "raw_dir":            "raw_dir",
            "processed_dir":      "processed_dir",
            "indices_computed":   "indices_computed",
        }
        sets  = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
        vals  = [status]

        for kwarg_name, col_name in allowed.items():
            if kwarg_name in kwargs:
                sets.append(f"{col_name} = ?")
                vals.append(kwargs[kwarg_name])

        vals.append(product_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE downloaded_products SET {', '.join(sets)} WHERE product_id = ?",
                vals
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Lectures (pour le dashboard Streamlit)
    # ------------------------------------------------------------------

    def count_downloaded(self) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM downloaded_products WHERE status != 'failed'"
            )
            return cur.fetchone()[0]

    def count_by_status(self) -> dict:
        """Retourne un dict {statut: nombre} pour le résumé dans les logs et le dashboard."""
        with self._connect() as conn:
            cur = conn.execute(
                """SELECT status, COUNT(*) as n
                   FROM downloaded_products
                   GROUP BY status ORDER BY n DESC"""
            )
            return {row["status"]: row["n"] for row in cur.fetchall()}

    def get_all_scenes(self, limit: int = 1000) -> list:
        """Retourne toutes les scènes pour le dashboard (tableau de bord)."""
        with self._connect() as conn:
            cur = conn.execute(
                """SELECT * FROM downloaded_products
                   ORDER BY downloaded_at DESC LIMIT ?""",
                (limit,)
            )
            return [dict(row) for row in cur.fetchall()]

    def get_stats(self) -> dict:
        """KPIs pour l'en-tête du dashboard."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM downloaded_products").fetchone()[0]
            validated = conn.execute(
                "SELECT COUNT(*) FROM downloaded_products WHERE status IN ('validated','indices_computed','archived')"
            ).fetchone()[0]
            rejected = conn.execute(
                "SELECT COUNT(*) FROM downloaded_products WHERE status LIKE 'rejected%'"
            ).fetchone()[0]
            with_indices = conn.execute(
                "SELECT COUNT(*) FROM downloaded_products WHERE indices_computed IS NOT NULL AND indices_computed != ''"
            ).fetchone()[0]
            avg_cloud = conn.execute(
                "SELECT AVG(cloud_cover_scl) FROM downloaded_products WHERE cloud_cover_scl IS NOT NULL"
            ).fetchone()[0]

        return {
            "total":        total,
            "validated":    validated,
            "rejected":     rejected,
            "with_indices": with_indices,
            "rejection_rate": round(100 * rejected / total, 1) if total else 0,
            "avg_cloud_scl": round(avg_cloud, 1) if avg_cloud else None,
        }

    # ------------------------------------------------------------------
    # Journal des exécutions (pipeline_runs)
    # ------------------------------------------------------------------

    def start_run(self, triggered_by: str = "agent") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO pipeline_runs (triggered_by) VALUES (?)",
                (triggered_by,)
            )
            conn.commit()
            return cur.lastrowid

    def finish_run(self, run_id: int, **counters):
        fields = ", ".join(f"{k} = ?" for k in counters)
        vals   = list(counters.values()) + [run_id]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE pipeline_runs SET finished_at = CURRENT_TIMESTAMP, {fields} WHERE id = ?",
                vals
            )
            conn.commit()

    def get_recent_runs(self, limit: int = 20) -> list:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cur.fetchall()]
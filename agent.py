"""
agent.py — Agent principal v3 : pipeline complet multi-sources.

SOURCES INTÉGRÉES :
====================
  1. Copernicus CDSE  → Sentinel-2 L2A (10m) + Sentinel-1 GRD radar
  2. Microsoft MPC    → Landsat 8 (30m) + Landsat 9 (30m)

PIPELINE POUR CHAQUE IMAGE :
==============================
  ÉTAPE 1 — Recherche        (API STAC / OData)
  ÉTAPE 2 — Téléchargement   (HTTP streaming)
  ÉTAPE 3 — Validation       (nuages SCL/QA, résolution, intégrité)
  ÉTAPE 4 — Indices           (NDVI, NDWI, MNDWI, NDMI, SAVI, WDVI)
  ÉTAPE 5 — Stockage          (raw/ processed/ quarantine/)
  ÉTAPE 6 — Catalogue         (registry.sqlite3)

USAGE :
========
  python agent.py              → boucle infinie (polling toutes les 30min)
  python agent.py --once       → une seule passe complète
  python agent.py --once --dry-run → simulation sans téléchargement
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# ── Imports locaux ──────────────────────────────────────────────────────────
from auth import CDSEAuth
from catalog import search_products, extract_footprint
from downloader import download_product
from registry import Registry
from file_storage import StorageManager
from validator import validate_product
from indices import compute_indices, INDEX_CATALOG
from mpc_connector import process_landsat_mpc_watch
from config import (
    WATCHES,
    MPC_WATCHES,
    INITIAL_LOOKBACK_DAYS,
    MAX_RESULTS_PER_QUERY,
    DEFAULT_CLOUD_THRESHOLD,
    DEFAULT_MIN_VALID_PIXEL_RATIO,
    INDICES_TO_COMPUTE,
    CLEANUP_ZIP_AFTER_EXTRACTION,
)


# ===========================================================================
# LOGGING
# ===========================================================================

def setup_logging(log_dir: str, log_level: str = "INFO"):
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "agent.log")
    level    = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

logger = logging.getLogger("satellite_agent")


# ===========================================================================
# GESTION DE L'ÉTAT (timestamps de dernière vérification)
# ===========================================================================

def iso_format(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def get_last_check(state_path: str, watch_name: str) -> datetime:
    """Lit la date de la dernière vérification pour un watch."""
    path = os.path.join(state_path, f".last_check_{watch_name}")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return datetime.fromisoformat(f.read().strip())
        except ValueError:
            pass
    return datetime.now(timezone.utc) - timedelta(days=INITIAL_LOOKBACK_DAYS)


def set_last_check(state_path: str, watch_name: str, dt: datetime):
    """Sauvegarde la date de la dernière vérification."""
    os.makedirs(state_path, exist_ok=True)
    path = os.path.join(state_path, f".last_check_{watch_name}")
    with open(path, "w") as f:
        f.write(dt.isoformat())


# ===========================================================================
# PIPELINE SENTINEL (CDSE) — une image
# ===========================================================================

def process_sentinel_product(product: dict, watch: dict,
                              auth: CDSEAuth, registry: Registry,
                              storage: StorageManager,
                              dry_run: bool = False) -> str:
    """
    Pipeline complet pour une image Sentinel-1 ou Sentinel-2 depuis CDSE.
    Retourne le statut final de la scène.
    """
    product_id     = product["Id"]
    product_name   = product["Name"]
    collection     = watch["collection"]
    watch_name     = watch["name"]
    cloud_thr      = watch.get("cloud_threshold", DEFAULT_CLOUD_THRESHOLD)
    content_date   = product.get("ContentDate", {}).get("Start", "")
    cloud_provider = product.get("cloud_cover_provider")

    tmp_dir = storage.root / "tmp" / collection / watch_name
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        logger.info(f"[DRY-RUN] Serait traitée : {product_name}")
        return "dry_run"

    # ÉTAPE 1 — Téléchargement
    logger.info(f"▶ 1/4 Téléchargement : {product_name}")
    try:
        zip_path = download_product(product_id, product_name, str(tmp_dir), auth)
    except RuntimeError as exc:
        logger.error(f"Échec téléchargement : {exc}")
        registry.mark_downloaded(
            product_id, product_name, collection, watch_name,
            content_date, None, status="failed"
        )
        return "failed"

    registry.mark_downloaded(
        product_id, product_name, collection, watch_name,
        content_date, zip_path, status="downloaded",
        cloud_cover_provider=cloud_provider,
        footprint_geojson=json.dumps(extract_footprint(product)),
    )

    # ÉTAPE 2 — Extraction + Validation nuages
    logger.info(f"▶ 2/4 Validation nuages (seuil {cloud_thr}%) : {product_name}")
    validation = validate_product(
        zip_path=zip_path,
        cloud_threshold=cloud_thr,
        min_valid_pixel_ratio=DEFAULT_MIN_VALID_PIXEL_RATIO,
        cloud_cover_provider=cloud_provider,
        collection=collection,
    )

    if validation["status"] in ("failed", "rejected_corrupt"):
        registry.update_status(product_id, validation["status"],
                               reason=validation["rejection_reason"])
        return validation["status"]

    if validation["status"] == "rejected_cloud":
        if validation["safe_dir"]:
            storage.move_to_quarantine(
                validation["safe_dir"], collection, watch_name,
                product_name, content_date
            )
        if CLEANUP_ZIP_AFTER_EXTRACTION:
            storage.cleanup_zip(zip_path)
        registry.update_status(
            product_id, "rejected_cloud",
            cloud_cover_scl=validation["cloud_cover_scl"],
            valid_pixel_ratio=validation["valid_pixel_ratio"],
            reason=validation["rejection_reason"],
        )
        logger.warning(f"☁️  REJETÉE : {product_name} — {validation['rejection_reason']}")
        return "rejected_cloud"

    # Image validée → déplacer vers raw/
    old_safe_dir = validation["safe_dir"]
    raw_dir = storage.move_extracted_to_raw(
        old_safe_dir, collection, watch_name, product_name, content_date
    )

    # Le fichier .SAFE a été déplacé : on recalcule le chemin du preview PNG
    # pour qu'il pointe vers son nouvel emplacement dans raw/
    preview_png_final = None
    if validation.get("preview_png"):
        try:
            rel_path = Path(validation["preview_png"]).relative_to(old_safe_dir)
            preview_png_final = str(Path(raw_dir) / rel_path)
        except ValueError:
            preview_png_final = validation["preview_png"]  # au cas où, chemin inchangé

     # Les bandes spectrales ont elles aussi été déplacées avec le dossier
    # .SAFE : on recalcule leurs chemins pour qu'ils pointent vers raw_dir,
    # sinon le calcul des indices (étape suivante) échoue avec
    # "No such file or directory" car il cherche encore dans l'ancien
    # dossier tmp/extracted qui n'existe plus.
    updated_bands = {}
    for alias, band_path in validation["bands"].items():
        try:
            rel_path = Path(band_path).relative_to(old_safe_dir)
            updated_bands[alias] = str(Path(raw_dir) / rel_path)
        except ValueError:
            updated_bands[alias] = band_path  # au cas où, chemin inchangé
    validation["bands"] = updated_bands

    if CLEANUP_ZIP_AFTER_EXTRACTION:
        storage.cleanup_zip(zip_path)

    registry.update_status(
        product_id, "validated",
        cloud_cover_scl=validation["cloud_cover_scl"],
        valid_pixel_ratio=validation["valid_pixel_ratio"],
        raw_dir=raw_dir,
        preview_png=preview_png_final,
    )
    logger.info(
        f"✅ VALIDÉE : nuages={validation['cloud_cover_scl']:.1f}% | "
        f"pixels valides={validation['valid_pixel_ratio']:.1%}"
    )

        # ÉTAPE 3 — Calcul des indices (uniquement produits optiques)
    is_optical = collection == "SENTINEL-2"
    if is_optical and INDICES_TO_COMPUTE and validation["bands"]:
        logger.info(f"▶ 3/4 Calcul indices {INDICES_TO_COMPUTE}")
        processed_dir = storage.get_processed_dir(
            collection, watch_name, product_name, content_date
        )
        index_results = compute_indices(
            bands=validation["bands"],
            output_dir=str(processed_dir),
            requested=INDICES_TO_COMPUTE,
        )
        computed = list(index_results.keys())

        # Avant de supprimer le .SAFE brut, on met l'aperçu quicklook à l'abri
        # dans processed_dir (sinon il disparaît avec raw_dir).
        preview_final = preview_png_final
        if preview_png_final and Path(preview_png_final).exists():
            try:
                import shutil
                dest_preview = Path(processed_dir) / "quick-look.png"
                shutil.copy2(preview_png_final, dest_preview)
                preview_final = str(dest_preview)
            except Exception as exc:
                logger.warning(f"Impossible de copier l'aperçu avant nettoyage : {exc}")

        registry.update_status(
            product_id, "indices_computed",
            processed_dir=str(processed_dir),
            indices_computed=",".join(computed),
            preview_png=preview_final,
        )
        logger.info(f"✅ Indices calculés : {computed}")

        # Nettoyage : le .SAFE brut n'est plus nécessaire une fois les
        # indices calculés avec succès — on le supprime pour économiser
        # l'espace disque (les .tif traités dans processed_dir suffisent
        # désormais pour le chatbot et les rapports PNG/PDF).
        if computed:
            try:
                import shutil
                shutil.rmtree(raw_dir, ignore_errors=False)
                logger.info(f"🗑️  Dossier brut supprimé après calcul des indices : {raw_dir}")
                registry.update_status(product_id, "indices_computed", raw_dir=None)
            except Exception as exc:
                logger.warning(f"Impossible de supprimer le dossier brut {raw_dir} : {exc}")
    else:
        if not is_optical:
            logger.info("3/4 Ignorée — produit radar (pas d'indices optiques)")

    # ÉTAPE 4 — Archivage
    logger.info(f"▶ 4/4 Archivage : {product_name}")
    registry.update_status(product_id, "archived")
    logger.info(f"✅ ARCHIVÉE : {product_name}\n")
    return "archived"


# ===========================================================================
# PIPELINE LANDSAT (MPC) — un watch complet
# ===========================================================================

def process_mpc_watch(watch: dict, registry: Registry,
                      storage: StorageManager, state_path: str,
                      dry_run: bool = False) -> dict:
    """
    Pipeline complet pour un watch Landsat via Microsoft Planetary Computer.
    Intègre les résultats dans le registre SQLite et le stockage organisé.
    """
    name     = watch["name"]
    now      = datetime.now(timezone.utc)
    start    = get_last_check(state_path, f"mpc_{name}")
    date_end   = now.strftime("%Y-%m-%d")
    date_start = start.strftime("%Y-%m-%d")
    satellite  = watch.get("satellite", "landsat8")
    cloud_thr  = watch.get("cloud_threshold", DEFAULT_CLOUD_THRESHOLD)

    logger.info(f"\n{'='*60}")
    logger.info(f"Watch MPC '{name}' | {satellite.upper()} | depuis {start.date()}")
    logger.info(f"{'='*60}")

    counters = {
        "found": 0, "downloaded": 0, "validated": 0,
        "rejected": 0, "indices_computed": 0, "failed": 0,
    }

    if dry_run:
        logger.info(f"[DRY-RUN] Watch MPC '{name}' ignoré en mode dry-run")
        set_last_check(state_path, f"mpc_{name}", now)
        return counters

    # Dossier temporaire pour les téléchargements MPC
    tmp_dir = storage.root / "tmp" / "landsat" / name

    # Traitement via mpc_connector
    results = process_landsat_mpc_watch(
        watch=watch,
        date_start=date_start,
        date_end=date_end,
        destination_dir=tmp_dir,
        cloud_threshold=cloud_thr,
    )

    counters["found"] = len(results)

    for r in results:
        scene_id     = r["scene_id"]
        content_date = r.get("content_date", "")
        collection   = f"LANDSAT_{satellite.upper()}"

        # Vérifier si déjà traité
        if registry.already_processed(scene_id):
            logger.info(f"Déjà traitée, ignorée : {scene_id}")
            continue

        # Enregistrer dans le catalogue
        registry.mark_downloaded(
            product_id=scene_id,
            name=scene_id,
            collection=collection,
            watch_name=name,
            content_date=content_date,
            file_path=r.get("safe_dir"),
            status=r["status"],
            cloud_cover_provider=r.get("cloud_cover_provider"),
        )

        if r["status"] == "failed":
            counters["failed"] += 1
            continue

        counters["downloaded"] += 1

        if r["status"] == "rejected_cloud":
            counters["rejected"] += 1
            registry.update_status(
                scene_id, "rejected_cloud",
                cloud_cover_scl=r.get("cloud_cover_scl"),
                valid_pixel_ratio=r.get("valid_pixel_ratio"),
                reason=r.get("rejection_reason"),
            )
            continue

        # Image validée
        counters["validated"] += 1

        # Déplacer vers raw/ organisé
        if r.get("safe_dir") and Path(r["safe_dir"]).exists():
            raw_dir = storage.move_extracted_to_raw(
                r["safe_dir"], collection, name, scene_id, content_date
            )
            registry.update_status(
                scene_id, "validated",
                cloud_cover_scl=r.get("cloud_cover_scl"),
                valid_pixel_ratio=r.get("valid_pixel_ratio"),
                raw_dir=raw_dir,
            )

        # Calcul des indices (Landsat = optique → indices disponibles)
        if r.get("bands") and INDICES_TO_COMPUTE:
            logger.info(f"▶ Calcul indices Landsat : {INDICES_TO_COMPUTE}")
            processed_dir = storage.get_processed_dir(
                collection, name, scene_id, content_date
            )
            index_results = compute_indices(
                bands=r["bands"],
                output_dir=str(processed_dir),
                requested=INDICES_TO_COMPUTE,
            )
            computed = list(index_results.keys())
            counters["indices_computed"] += len(computed)
            registry.update_status(
                scene_id, "indices_computed",
                processed_dir=str(processed_dir),
                indices_computed=",".join(computed),
            )
            logger.info(f"✅ Indices Landsat calculés : {computed}")

        registry.update_status(scene_id, "archived")

    set_last_check(state_path, f"mpc_{name}", now)
    logger.info(
        f"Watch MPC '{name}' terminé → "
        f"{counters['found']} trouvées | "
        f"{counters['validated']} validées | "
        f"{counters['rejected']} rejetées"
    )
    return counters


# ===========================================================================
# PIPELINE SENTINEL — un watch complet
# ===========================================================================

def process_sentinel_watch(watch: dict, auth: CDSEAuth,
                           registry: Registry, storage: StorageManager,
                           state_path: str, dry_run: bool = False) -> dict:
    """Traite toutes les nouvelles images Sentinel pour un watch donné."""
    name = watch["name"]
    now  = datetime.now(timezone.utc)
    start = get_last_check(state_path, name)

    logger.info(f"\n{'='*60}")
    logger.info(f"Watch '{name}' | {watch['collection']} | depuis {start.date()}")
    logger.info(f"{'='*60}")

    products = search_products(
        collection=watch["collection"],
        bbox=watch["bbox"],
        start_iso=iso_format(start),
        end_iso=iso_format(now),
        product_type=watch.get("product_type"),
        max_cloud_cover=watch.get("max_cloud_cover"),
        top=MAX_RESULTS_PER_QUERY,
    )

    counters = {
        "found": len(products), "skipped": 0,
        "downloaded": 0, "validated": 0,
        "rejected": 0, "failed": 0,
    }

    for product in products:
        pid = product["Id"]
        if registry.already_processed(pid):
            counters["skipped"] += 1
            continue

        status = process_sentinel_product(
            product, watch, auth, registry, storage, dry_run=dry_run
        )

        if status == "archived":
            counters["downloaded"] += 1
            counters["validated"]  += 1
        elif status == "validated":
            counters["downloaded"] += 1
            counters["validated"]  += 1
        elif status in ("rejected_cloud", "rejected_corrupt"):
            counters["downloaded"] += 1
            counters["rejected"]   += 1
        elif status == "failed":
            counters["failed"] += 1

    set_last_check(state_path, name, now)
    logger.info(
        f"Watch '{name}' terminé → "
        f"{counters['found']} trouvées | "
        f"{counters['skipped']} ignorées | "
        f"{counters['downloaded']} téléchargées | "
        f"{counters['validated']} validées | "
        f"{counters['rejected']} rejetées"
    )
    return counters


# ===========================================================================
# UNE PASSE COMPLÈTE (toutes sources)
# ===========================================================================

def run_once(auth: CDSEAuth, registry: Registry,
             storage: StorageManager, state_path: str,
             dry_run: bool = False):
    """
    Exécute une passe complète sur TOUTES les sources :
      - Sentinel-1/2 via CDSE (WATCHES)
      - Landsat 8/9  via MPC  (MPC_WATCHES)
    """
    run_id = registry.start_run(
        triggered_by="dry_run" if dry_run else "agent"
    )

    total = {
        "found": 0, "downloaded": 0, "validated": 0,
        "rejected": 0, "indices_computed": 0, "failed": 0,
    }

    # ── 1. Sentinel-1 et Sentinel-2 (CDSE) ──────────────────────────────
    logger.info("\n" + "█"*60)
    logger.info("  SOURCE 1 : Copernicus CDSE (Sentinel-1/2)")
    logger.info("█"*60)

    for watch in WATCHES:
        try:
            c = process_sentinel_watch(
                watch, auth, registry, storage, state_path, dry_run=dry_run
            )
            for k in ("found", "downloaded", "validated", "rejected", "failed"):
                total[k] += c.get(k, 0)
        except Exception:
            logger.exception(f"Erreur sur le watch Sentinel '{watch['name']}'")

    # ── 2. Landsat 8 et Landsat 9 (Microsoft Planetary Computer) ────────
    logger.info("\n" + "█"*60)
    logger.info("  SOURCE 2 : Microsoft Planetary Computer (Landsat 8/9)")
    logger.info("█"*60)

    for watch in MPC_WATCHES:
        try:
            c = process_mpc_watch(
                watch, registry, storage, state_path, dry_run=dry_run
            )
            for k in ("found", "downloaded", "validated",
                      "rejected", "indices_computed", "failed"):
                total[k] += c.get(k, 0)
        except Exception:
            logger.exception(f"Erreur sur le watch MPC '{watch['name']}'")

    # ── Résumé ────────────────────────────────────────────────────────────
    usage = storage.get_usage_summary()
    logger.info(
        f"\n{'='*60}\n"
        f"RÉSUMÉ DE LA PASSE\n"
        f"  Images trouvées    : {total['found']}\n"
        f"  Téléchargées       : {total['downloaded']}\n"
        f"  ✅ Validées        : {total['validated']}\n"
        f"  ☁️  Rejetées       : {total['rejected']}\n"
        f"  📊 Indices calculés : {total['indices_computed']}\n"
        f"  ❌ Échouées        : {total['failed']}\n"
        f"  💾 Stockage raw    : {usage.get('raw',{}).get('size_mb',0)} Mo\n"
        f"  💾 Stockage indice : {usage.get('processed',{}).get('size_mb',0)} Mo\n"
        f"  🗄️  Quarantaine    : {usage.get('quarantine',{}).get('size_mb',0)} Mo\n"
        f"  Registre           : {registry.count_by_status()}\n"
        f"{'='*60}\n"
    )

    registry.finish_run(
        run_id,
        scenes_found=total["found"],
        downloaded=total["downloaded"],
        validated=total["validated"],
        rejected=total["rejected"],
        indices_done=total["indices_computed"],
        failed=total["failed"],
        status="success",
    )
    return total


# ===========================================================================
# POINT D'ENTRÉE
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Agent IA images satellitaires — DGRE"
    )
    parser.add_argument("--once", action="store_true",
                        help="Exécute une seule passe puis quitte")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simule sans télécharger")
    args = parser.parse_args()

    load_dotenv()

    storage_root  = os.getenv("STORAGE_ROOT", "./data")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "1800"))
    log_level     = os.getenv("LOG_LEVEL", "INFO")
    state_path    = os.path.join(storage_root, "_state")

    setup_logging(os.path.join(storage_root, "logs"), log_level)

    logger.info("=" * 60)
    logger.info("   AGENT IA IMAGES SATELLITAIRES — DGRE v3")
    logger.info("   Sous-direction de l'eau de surface")
    logger.info("=" * 60)
    logger.info(f"Stockage         : {os.path.abspath(storage_root)}")
    logger.info(f"Watches Sentinel : {[w['name'] for w in WATCHES]}")
    logger.info(f"Watches Landsat  : {[w['name'] for w in MPC_WATCHES]}")
    logger.info(f"Indices          : {INDICES_TO_COMPUTE}")
    if args.dry_run:
        logger.info("*** MODE DRY-RUN — aucun téléchargement ***")

    # Initialisation stockage
    storage = StorageManager(storage_root)
    ok, msg = storage.is_writable()
    if not ok:
        logger.error(f"Stockage inaccessible : {msg}")
        sys.exit(1)
    logger.info(f"Stockage OK : {msg}")

    # Initialisation registre
    registry = Registry(
        db_path=os.path.join(storage_root, "registry.sqlite3")
    )

    # Authentification CDSE (Sentinel)
    username = os.getenv("CDSE_USERNAME")
    password = os.getenv("CDSE_PASSWORD")
    try:
        auth = CDSEAuth(username, password)
        auth.get_token()
        logger.info("Authentification Copernicus CDSE OK")
    except Exception as exc:
        logger.error(f"Impossible de s'authentifier sur CDSE : {exc}")
        sys.exit(1)

    # Note : MPC ne nécessite aucune authentification
    logger.info("Microsoft Planetary Computer : accès public (pas d'auth requise)")

    if args.once or args.dry_run:
        run_once(auth, registry, storage, state_path, dry_run=args.dry_run)
        return

    # Boucle infinie (mode agent continu)
    while True:
        try:
            run_once(auth, registry, storage, state_path)
        except Exception:
            logger.exception("Erreur dans la boucle principale")
        logger.info(f"Pause de {poll_interval}s...")
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
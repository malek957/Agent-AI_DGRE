"""
downloader.py — Téléchargement des fichiers produits depuis Copernicus Data Space
Ecosystem, avec retry automatique en cas d'erreur réseau ou de token expiré.

CORRECTIONS INTÉGRITÉ :
========================
Un flux réseau qui s'interrompt en cours de route (proxy, VPN, coupure Wi-Fi...)
ne lève pas toujours une exception côté `requests` — `iter_content()` peut
simplement s'arrêter, et la boucle `for` se termine "normalement". Sans
vérification, le fichier partiel était renommé comme s'il était complet, et
comme `os.path.exists(dest_path)` suffisait à "sauter" un re-téléchargement,
un fichier corrompu restait piégé définitivement, jamais retenté.

Ce module vérifie maintenant, avant de considérer un téléchargement réussi :
  1. La taille réellement écrite correspond au Content-Length annoncé
     (quand le serveur le fournit)
  2. Le fichier est un ZIP valide et lisible (zipfile.is_zipfile)
Et avant de sauter un téléchargement déjà présent sur disque, on revérifie
la même intégrité — un fichier existant mais corrompu est supprimé et
re-téléchargé automatiquement.
"""

import os
import time
import logging
import zipfile
import requests

logger = logging.getLogger("satellite_agent.downloader")

ZIPPER_URL_TEMPLATE = "https://zipper.dataspace.copernicus.eu/odata/v1/Products({product_id})/$value"

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 10

# En dessous de cette taille, un produit Sentinel-1/2 est presque certainement
# une réponse d'erreur (JSON/HTML) sauvegardée par erreur, pas un vrai produit.
# Filet de sécurité en complément de la vérification Content-Length.
MIN_VALID_SIZE_BYTES = 5 * 1024 * 1024  # 5 Mo


def _is_valid_zip(path: str) -> bool:
    """Vérifie que le fichier est un ZIP lisible (pas juste présent sur disque)."""
    try:
        if not os.path.exists(path):
            return False
        if os.path.getsize(path) < MIN_VALID_SIZE_BYTES:
            return False
        return zipfile.is_zipfile(path)
    except Exception:
        return False


def download_product(product_id: str, name: str, dest_dir: str, auth) -> str:
    """
    Télécharge un produit (fichier .zip) vers dest_dir.

    Args:
        product_id: l'identifiant UUID du produit (champ "Id" de l'API catalogue)
        name: nom du produit (utilisé pour nommer le fichier)
        dest_dir: dossier de destination
        auth: instance de CDSEAuth (fournit le token)

    Returns:
        Le chemin du fichier téléchargé (garanti être un ZIP valide et complet).

    Raises:
        RuntimeError si le téléchargement échoue après tous les essais.
    """
    os.makedirs(dest_dir, exist_ok=True)
    safe_name = name if name.endswith(".zip") else f"{name}.zip"
    dest_path = os.path.join(dest_dir, safe_name)
    tmp_path = dest_path + ".part"

    if os.path.exists(dest_path):
        if _is_valid_zip(dest_path):
            logger.info(f"Fichier déjà présent et valide, on ignore : {safe_name}")
            return dest_path
        else:
            logger.warning(
                f"Fichier existant invalide/corrompu ({safe_name}) — "
                f"suppression et re-téléchargement."
            )
            os.remove(dest_path)

    url = ZIPPER_URL_TEMPLATE.format(product_id=product_id)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        # Toujours repartir d'un fichier partiel propre à chaque essai
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

        try:
            token = auth.get_token()
            headers = {"Authorization": f"Bearer {token}"}

            logger.info(f"Téléchargement (essai {attempt}/{MAX_RETRIES}): {safe_name}")
            with requests.get(url, headers=headers, stream=True, timeout=300) as response:
                if response.status_code == 401:
                    # Token invalide/expiré — on force un renouvellement et on retente
                    logger.warning("Token rejeté (401), renouvellement et nouvel essai...")
                    auth.get_token()
                    continue

                response.raise_for_status()

                expected_size = response.headers.get("Content-Length")
                expected_size = int(expected_size) if expected_size else None
                content_type = response.headers.get("Content-Type", "")

                written = 0
                with open(tmp_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)

                # ── Vérification 1 : taille annoncée vs taille réellement reçue ──
                if expected_size is not None and written != expected_size:
                    last_error = (
                        f"Téléchargement incomplet : {written} octets reçus "
                        f"sur {expected_size} attendus (flux coupé prématurément)"
                    )
                    logger.warning(f"⚠️ Essai {attempt}/{MAX_RETRIES} — {last_error}")
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue

                # ── Vérification 2 : taille minimale plausible ──
                if written < MIN_VALID_SIZE_BYTES:
                    last_error = (
                        f"Fichier trop petit ({written} octets, "
                        f"Content-Type={content_type!r}) — probablement une "
                        f"réponse d'erreur du serveur plutôt qu'un vrai produit"
                    )
                    logger.warning(f"⚠️ Essai {attempt}/{MAX_RETRIES} — {last_error}")
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue

                # ── Vérification 3 : c'est un vrai ZIP lisible ──
                if not zipfile.is_zipfile(tmp_path):
                    last_error = (
                        f"Le fichier téléchargé n'est pas un ZIP valide "
                        f"({written} octets, Content-Type={content_type!r})"
                    )
                    logger.warning(f"⚠️ Essai {attempt}/{MAX_RETRIES} — {last_error}")
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue

                os.rename(tmp_path, dest_path)
                size_mb = os.path.getsize(dest_path) / (1024 * 1024)
                logger.info(f"✅ Téléchargement terminé et vérifié : {safe_name} ({size_mb:.1f} Mo)")
                return dest_path

        except requests.exceptions.RequestException as e:
            last_error = e
            logger.warning(f"Échec essai {attempt}/{MAX_RETRIES} pour {safe_name}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)  # backoff progressif

    # Tous les essais ont échoué — on nettoie le fichier partiel orphelin
    # pour ne pas laisser traîner de faux dossiers/fichiers vides sur disque.
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    raise RuntimeError(
        f"Impossible de télécharger {safe_name} après {MAX_RETRIES} essais. "
        f"Dernière erreur: {last_error}"
    )
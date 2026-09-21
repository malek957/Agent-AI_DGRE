"""
chatbot_engine.py — Cerveau du chatbot intelligent DGRE (version corrigée).

CORRECTIONS v2 :
- Téléchargement automatique déclenché dès qu'une image est manquante
- Lancement en arrière-plan (non bloquant)
- Cache du registre pour accélérer les réponses
"""

import os
import sys
import json
import time
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))
from registry import Registry
from indices import compute_indices
from validator import _find_bands
from file_storage import StorageManager
from config import INDICES_TO_COMPUTE

logger = logging.getLogger("satellite_agent.chatbot")

DB_PATH      = os.getenv("STORAGE_ROOT", "./data") + "/registry.sqlite3"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
_storage = StorageManager(os.getenv("STORAGE_ROOT", "./data"))

# Dossier où sont stockés les aperçus PNG générés à partir des .tif (cache)
PREVIEW_CACHE_DIR = Path(os.getenv("STORAGE_ROOT", "./data")) / "previews"
PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Mots-clés utilisés pour deviner le contenu d'un .tif à partir de son nom
# ORDRE IMPORTANT : "mndwi" doit être testé AVANT "ndwi", car "mndwi"
# contient "ndwi" comme sous-chaîne (sinon MNDWI.tif serait classé NDWI).
INDEX_KEYWORDS = ["mndwi", "ndvi", "ndwi", "ndmi", "savi", "wdvi", "evi"]
TRUE_COLOR_KEYWORDS = ["true_color", "truecolor", "rgb", "tci", "composite", "natural"]

INDEX_COLORMAPS = {
    "ndvi": "RdYlGn",
    "ndwi": "Blues",
    "mndwi": "Blues",
    "ndmi": "BrBG",
    "savi": "RdYlGn",
    "wdvi": "RdYlGn",
    "evi": "RdYlGn",
}

ZONES_MAPPING = {
    "tunis":           "tunis_sentinel2",
    "gouvernorat":     "tunis_sentinel2",
    "sidi salem":      "barrage_sidi_salem",
    "barrage":         "barrage_sidi_salem",
    "nebhana":         "tunisie_complete",
    "medjerda":        "tunisie_complete",
    "tunisie":         "tunisie_complete",
    "sfax":            "tunisie_complete",
    "sousse":          "tunisie_complete",
    "landsat":         "tunisie_landsat9",
    "landsat 8":       "tunisie_landsat8",
    "landsat 9":       "tunisie_landsat9",
}

# Cache du registre (évite de recharger à chaque message)
_registry_cache = {"context": "", "timestamp": 0}


# ===========================================================================
# CHARGEMENT DU CONTEXTE DU REGISTRE (avec cache 60 secondes)
# ===========================================================================

def get_registry_context() -> str:
    """Charge le registre SQLite et le met en cache 60 secondes."""
    global _registry_cache

    # Utiliser le cache si moins de 60 secondes
    if time.time() - _registry_cache["timestamp"] < 60 and _registry_cache["context"]:
        return _registry_cache["context"]

    if not Path(DB_PATH).exists():
        return "Le catalogue est vide. Aucune image n'a encore été téléchargée."

    try:
        reg    = Registry(db_path=DB_PATH)
        scenes = reg.get_all_scenes(limit=100)
        stats  = reg.get_stats()

        context = f"""
CATALOGUE DES IMAGES SATELLITAIRES DGRE :
- Total : {stats.get('total', 0)} images
- Validées/archivées : {stats.get('validated', 0)}
- Rejetées (nuages) : {stats.get('rejected', 0)}
- Avec indices : {stats.get('with_indices', 0)}
- Taux de rejet : {stats.get('rejection_rate', 0)}%

IMAGES DISPONIBLES :
"""
        for s in scenes[:80]:
            cloud = s.get("cloud_cover_scl")
            cloud_txt = f"{cloud:.1f}%" if cloud is not None else "N/A"
            date_raw  = s.get("content_date", "?")
            try:
                dt       = datetime.fromisoformat(date_raw[:10])
                date_fmt = dt.strftime("%d/%m/%Y")
            except Exception:
                date_fmt = date_raw[:10] if date_raw else "?"

            context += (
                f"\n• {s.get('name','?')[:50]}"
                f" | {s.get('collection','?')}"
                f" | zone={s.get('watch_name','?')}"
                f" | date={date_fmt}"
                f" | statut={s.get('status','?')}"
                f" | nuages={cloud_txt}"
                f" | indices={s.get('indices_computed','—') or '—'}"
                f" | dossier={s.get('processed_dir','N/A') or 'N/A'}"
            )

        # Mettre en cache
        _registry_cache["context"]   = context
        _registry_cache["timestamp"] = time.time()
        return context

    except Exception as exc:
        return f"Erreur chargement catalogue : {exc}"


# ===========================================================================
# APERÇUS VISUELS DES SCÈNES (composite couleur + carte d'indice)
# ===========================================================================

def _classify_tif(filename: str) -> str:
    """Devine le type d'un fichier .tif à partir de son nom."""
    name = filename.lower()
    for kw in INDEX_KEYWORDS:
        if kw in name:
            return kw
    for kw in TRUE_COLOR_KEYWORDS:
        if kw in name:
            return "true_color"
    return "autre"


def _make_preview_png(tif_path: Path, kind: str):
    """
    Convertit un .tif en aperçu .png affichable (mis en cache).
    - true_color -> composite RGB avec étirement de contraste
    - indice     -> image colorée avec la palette adaptée
    Retourne le chemin du PNG, ou None si la conversion échoue.
    """
    preview_path = PREVIEW_CACHE_DIR / f"{tif_path.stem}_{kind}.png"
    if preview_path.exists() and preview_path.stat().st_mtime >= tif_path.stat().st_mtime:
        return preview_path  # aperçu déjà généré et à jour

    try:
        import numpy as np
        import rasterio
        import matplotlib
        from PIL import Image

        with rasterio.open(tif_path) as src:
            if kind == "true_color" and src.count >= 3:
                arr = src.read([1, 2, 3]).astype("float32")
                p2, p98 = np.nanpercentile(arr, [2, 98])
                arr = np.clip((arr - p2) / (p98 - p2 + 1e-9), 0, 1)
                img = (np.transpose(arr, (1, 2, 0)) * 255).astype("uint8")
                Image.fromarray(img).save(preview_path)
            else:
                import matplotlib.cm as cm
                band = src.read(1).astype("float32")
                band = np.where(np.isfinite(band), band, np.nan)
                vmin, vmax = np.nanpercentile(band, [2, 98])
                norm = np.clip((band - vmin) / (vmax - vmin + 1e-9), 0, 1)
                norm = np.nan_to_num(norm, nan=0.0)
                cmap = matplotlib.colormaps[INDEX_COLORMAPS.get(kind, "viridis")]
                rgba = cmap(norm)
                img = (rgba[:, :, :3] * 255).astype("uint8")
                Image.fromarray(img).save(preview_path)

        return preview_path

    except ImportError:
        logger.warning("rasterio / matplotlib / Pillow manquants pour générer les aperçus.")
        return None
    except Exception as exc:
        logger.warning(f"Aperçu impossible pour {tif_path.name} : {exc}")
        return None


def _describe_scene(scene: dict) -> str:
    """Description courte et lisible d'une scène, pour affichage au technicien."""
    cloud = scene.get("cloud_cover_scl")
    cloud_txt = f"{cloud:.1f}% de nuages" if cloud is not None else "nuages non évalués"
    date_raw = scene.get("content_date", "?")
    try:
        date_fmt = datetime.fromisoformat(date_raw[:10]).strftime("%d/%m/%Y")
    except Exception:
        date_fmt = date_raw[:10] if date_raw else "date inconnue"

    return (
        f"{scene.get('collection','?')} du {date_fmt} — "
        f"zone {scene.get('watch_name','?')} — {cloud_txt}. "
        f"Indices calculés : {scene.get('indices_computed') or 'aucun'}. "
        f"Statut : {scene.get('status','?')}."
    )

def _locate_true_safe_dir(raw_dir: Path) -> Path:
    """
    Certains dossiers raw/ contiennent un niveau d'imbrication en trop
    (bug historique de déplacement de dossier : <nom>.SAFE/<nom>.SAFE/GRANULE/...
    au lieu de <nom>.SAFE/GRANULE/...). On cherche le vrai dossier qui
    contient directement GRANULE/, à n'importe quelle profondeur.
    """
    if (raw_dir / "GRANULE").exists():
        return raw_dir
    for candidate in raw_dir.rglob("GRANULE"):
        return candidate.parent
    return raw_dir  # rien trouvé, on retourne tel quel (échouera proprement)


def ensure_indices_computed(scene: dict, reg: Registry) -> dict:
    """
    Si les indices n'ont pas encore été calculés pour cette scène, mais que
    le dossier brut (.SAFE) existe déjà sur le disque, on les calcule
    maintenant, à la demande — sans repasser par un téléchargement.
    """
    # Le dossier processed_dir peut exister mais être vide (créé lors d'une
    # tentative précédente échouée) — on vérifie donc la présence de vrais
    # fichiers .tif, pas seulement l'existence du dossier.
    processed_dir = scene.get("processed_dir")
    if processed_dir and Path(processed_dir).exists():
        if any(Path(processed_dir).glob("*.tif")):
            return scene

    raw_dir = scene.get("raw_dir")
    if not raw_dir or not Path(raw_dir).exists():
        return scene

    if scene.get("collection") == "SENTINEL-1":
        return scene

    true_safe_dir = _locate_true_safe_dir(Path(raw_dir))
    bands = _find_bands(true_safe_dir)
    if not bands:
        logger.warning(f"Aucune bande exploitable pour {scene.get('name')} (cherché dans {true_safe_dir})")
        return scene

    processed_dir = _storage.get_processed_dir(
        scene.get("collection"), scene.get("watch_name"),
        scene.get("name"), scene.get("content_date"),
    )
    try:
        index_results = compute_indices(
            bands=bands, output_dir=str(processed_dir),
            requested=INDICES_TO_COMPUTE,
        )
    except Exception as exc:
        logger.warning(f"Calcul indices à la demande échoué pour {scene.get('name')} : {exc}")
        return scene

    computed = list(index_results.keys())
    if computed:
        reg.update_status(
            scene.get("product_id") or scene.get("name"),
            "indices_computed",
            processed_dir=str(processed_dir),
            indices_computed=",".join(computed),
        )
        scene["processed_dir"] = str(processed_dir)
        scene["indices_computed"] = ",".join(computed)
        logger.info(f"✅ Indices calculés à la demande pour {scene.get('name')} : {computed}")

    return scene



def build_scene_media(scene: dict) -> dict:
    """
    Construit tout ce qu'il faut pour afficher une scène à l'utilisateur :
    - une description texte courte
    - des aperçus PNG (composite couleur + carte(s) d'indice, quel que soit l'indice)
    - la liste des .tif bruts, pour téléchargement
    - un rapport PNG (planche contact) et un rapport PDF téléchargeables,
      qui sont le vrai "résultat" livrable pour les ingénieurs/techniciens
    - l'aperçu PNG natif (quick-look) fourni tel quel par l'ESA, disponible
      immédiatement sans aucun traitement, même pour Sentinel-1 (radar) où
      les .tif d'indices n'existent pas
    """
    processed_dir = scene.get("processed_dir")
    media = {
        "description": _describe_scene(scene),
        "previews": [],       # [{"label": "NDVI", "png_path": ..., "tif_path": ...}]
        "raw_files": [],      # [{"name": ..., "path": ..., "kind": ...}]
        "report_png": None,   # chemin du rapport PNG (planche contact), ou None
        "report_pdf": None,   # chemin du rapport PDF, ou None
        "quicklook_png": None,  # aperçu natif ESA, tel quel, sans traitement
    }

    # Aperçu natif ESA — disponible dès la validation, même sans indices calculés
    quicklook = scene.get("preview_png")
    if quicklook and Path(quicklook).exists():
        media["quicklook_png"] = quicklook

    if not processed_dir or not Path(processed_dir).exists():
        return media

    for tif in sorted(Path(processed_dir).glob("*.tif")):
        kind = _classify_tif(tif.name)
        media["raw_files"].append({"name": tif.name, "path": str(tif), "kind": kind})

        png = _make_preview_png(tif, kind)
        if png:
            label = "Composite couleur" if kind == "true_color" else kind.upper()
            media["previews"].append({
                "label": label,
                "png_path": str(png),
                "tif_path": str(tif),
            })

    # Génération des rapports (résultat livrable, sans SIG)
    png_report = _export_scene_png(media, scene)
    if png_report:
        media["report_png"] = str(png_report)

    pdf_report = _export_scene_pdf(media, scene)
    if pdf_report:
        media["report_pdf"] = str(pdf_report)

    return media


# ===========================================================================
# EXPORT DU RÉSULTAT — PNG (planche contact) et PDF (rapport)
# ===========================================================================

REPORTS_DIR = Path(os.getenv("STORAGE_ROOT", "./data")) / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _safe_stem(scene: dict) -> str:
    """Nom de fichier sûr à partir du nom de la scène (pas de caractères spéciaux)."""
    name = scene.get("name", "scene")
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:60]


def _export_scene_png(media: dict, scene: dict):
    """
    Assemble une planche contact PNG unique : titre + composite couleur +
    carte(s) d'indice côte à côte avec légendes. Un seul fichier à
    télécharger/partager/imprimer — c'est le "résultat" que voit l'encadrante.
    """
    previews = media.get("previews", [])
    if not previews:
        return None

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning("Pillow manquant pour générer le rapport PNG.")
        return None

    thumb_size = 380
    padding    = 16
    title_h    = 80
    label_h    = 26

    imgs = []
    for prev in previews:
        try:
            im = Image.open(prev["png_path"]).convert("RGB")
            im.thumbnail((thumb_size, thumb_size))
            imgs.append((im, prev["label"]))
        except Exception as exc:
            logger.warning(f"Aperçu illisible pour le rapport PNG ({prev['png_path']}) : {exc}")

    if not imgs:
        return None

    n = len(imgs)
    cell_w   = thumb_size + padding
    cell_h   = thumb_size + label_h + padding
    canvas_w = cell_w * n + padding
    canvas_h = title_h + cell_h + padding

    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
    draw   = ImageDraw.Draw(canvas)

    try:
        font_title = ImageFont.truetype("DejaVuSans-Bold.ttf", 20)
        font_sub   = ImageFont.truetype("DejaVuSans.ttf", 13)
        font_label = ImageFont.truetype("DejaVuSans-Bold.ttf", 15)
    except Exception:
        font_title = font_sub = font_label = ImageFont.load_default()

    title = f"DGRE — {scene.get('collection','?')} — {scene.get('watch_name','?')}"
    draw.text((padding, 10), title[:110], fill="black", font=font_title)
    draw.text((padding, 40), media.get("description", "")[:150], fill="#444", font=font_sub)

    x, y = padding, title_h
    for im, label in imgs:
        canvas.paste(im, (x, y))
        draw.text((x, y + thumb_size + 4), label, fill="black", font=font_label)
        x += cell_w

    out_path = REPORTS_DIR / f"{_safe_stem(scene)}_rapport.png"
    try:
        canvas.save(out_path)
        return out_path
    except Exception as exc:
        logger.warning(f"Écriture du rapport PNG impossible : {exc}")
        return None


def _export_scene_pdf(media: dict, scene: dict):
    """
    Génère un rapport PDF une page : métadonnées de la scène + composite
    couleur + carte(s) d'indice, prêt à imprimer/partager sans SIG.
    Nécessite : pip install fpdf2
    """
    try:
        from fpdf import FPDF
    except ImportError:
        logger.warning("fpdf2 non installé — rapport PDF non généré (pip install fpdf2).")
        return None

    previews = media.get("previews", [])

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Rapport - Images Satellitaires DGRE", ln=True)

    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(60, 60, 60)
    # Le texte peut contenir des caractères non latin-1 (ex: guillemets) —
    # on nettoie pour éviter une erreur d'encodage avec fpdf "classique".
    description = media.get("description", "").encode("latin-1", "replace").decode("latin-1")
    pdf.multi_cell(0, 6, description)
    pdf.ln(2)

    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)

    if previews:
        cols          = 2
        img_w         = 90
        x_positions   = [10, 110]
        y0            = pdf.get_y()
        max_row_h     = 0
        col           = 0
        for prev in previews:
            x = x_positions[col]
            try:
                pdf.image(prev["png_path"], x=x, y=y0, w=img_w)
                label_y = y0 + img_w * 0.66 + 2
                pdf.set_xy(x, label_y)
                pdf.set_font("Helvetica", "B", 10)
                label = prev["label"].encode("latin-1", "replace").decode("latin-1")
                pdf.cell(img_w, 5, label, align="C")
                max_row_h = max(max_row_h, img_w * 0.66 + 8)
            except Exception as exc:
                logger.warning(f"Image illisible pour le rapport PDF ({prev['png_path']}) : {exc}")
            col += 1
            if col == cols:
                col = 0
                y0 += max_row_h + 6
                max_row_h = 0
                if y0 > 250:
                    pdf.add_page()
                    y0 = 20
    else:
        pdf.set_font("Helvetica", "I", 11)
        pdf.cell(0, 8, "Aucun apercu image disponible pour cette scene.", ln=True)

    out_path = REPORTS_DIR / f"{_safe_stem(scene)}_rapport.pdf"
    try:
        pdf.output(str(out_path))
        return out_path
    except Exception as exc:
        logger.warning(f"Écriture du rapport PDF impossible : {exc}")
        return None


# ===========================================================================
# EXTRACTION DES PARAMÈTRES
# ===========================================================================

def extract_search_params(user_message: str, groq_client) -> dict:
    """Extrait les paramètres de recherche depuis la question utilisateur."""
    today = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""Analyse ce message et retourne UNIQUEMENT un JSON valide sans texte avant ou après :

{{
  "zone_keyword": "mot-clé zone en minuscules (tunis/sidi salem/tunisie/sfax/landsat...)",
  "date_start": "YYYY-MM-DD ou null",
  "date_end": "YYYY-MM-DD ou null",
  "max_cloud": nombre 0-100 (défaut 20),
  "indices": [],
  "is_download_request": true
}}

Règles :
- is_download_request est TOUJOURS true si l'utilisateur veut voir/obtenir/télécharger une image
- Aujourd'hui : {today}
- Convertis les dates relatives (hier, ce mois...) en dates absolues

Message : "{user_message}"

JSON :"""

    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        logger.info(f"Réponse brute Groq (extraction paramètres) : {raw!r}") 

        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning(f"Extraction paramètres échouée : {exc}")
        return {
            "zone_keyword": "tunis",
            "date_start": None,
            "date_end": None,
            "max_cloud": 20,
            "indices": [],
            "is_download_request": True,
        }


# ===========================================================================
# RECHERCHE DANS LE REGISTRE
# ===========================================================================

def search_in_registry(params: dict) -> list:
    """Cherche dans le registre SQLite les images correspondant aux paramètres."""
    if not Path(DB_PATH).exists():
        return []

    reg    = Registry(db_path=DB_PATH)
    scenes = reg.get_all_scenes(limit=500)

    zone_kw    = params.get("zone_keyword", "").lower()
    max_cloud  = params.get("max_cloud", 100)

    # Trouver le watch_name correspondant
    watch_target = None
    for kw, watch in ZONES_MAPPING.items():
        if kw in zone_kw or zone_kw in kw:
            watch_target = watch
            break

    results = []
    for s in scenes:
        # Filtre zone
        if watch_target and s.get("watch_name", "") != watch_target:
            continue

        # Filtre date
        date_raw = s.get("content_date", "")
        if date_raw and params.get("date_start"):
            try:
                scene_date = datetime.fromisoformat(date_raw[:10])
                start      = datetime.fromisoformat(params["date_start"])
                end_str    = params.get("date_end") or params["date_start"]
                end        = datetime.fromisoformat(end_str) + timedelta(days=1)
                if not (start <= scene_date <= end):
                    continue
            except Exception:
                pass

        # Filtre nuages
        cloud = s.get("cloud_cover_scl")
        if cloud is not None and cloud > max_cloud:
            continue

        # Uniquement images valides
        if s.get("status") not in ("archived", "indices_computed", "validated"):
            continue

        results.append(s)

    # Trier par nuages croissants
    results.sort(key=lambda x: x.get("cloud_cover_scl") or 999)
    return results[:5]


# ===========================================================================
# TÉLÉCHARGEMENT AUTOMATIQUE (non bloquant)
# ===========================================================================

def trigger_download(params: dict) -> tuple:
    """
    Lance agent.py en arrière-plan et retourne immédiatement.
    L'utilisateur n'attend pas la fin du téléchargement.
    """
    zone_kw = params.get("zone_keyword", "tunis").lower()

    logger.info(f"Lancement téléchargement arrière-plan : zone={zone_kw}")

    try:
        agent_path = str(Path(__file__).parent / "agent.py")
        subprocess.Popen(
            [sys.executable, agent_path, "--once"],
            cwd=str(Path(__file__).parent),
        )
        return True, (
            "⏳ **L'agent de téléchargement a été lancé en arrière-plan.**\n\n"
            f"Je recherche les images pour la zone **{zone_kw}** "
            f"sur les sources Sentinel-2 (Copernicus) et Landsat (Microsoft).\n\n"
            "Le téléchargement prend environ **10 à 15 minutes**.\n\n"
            "➡️ **Revenez dans quelques minutes** et reposez votre question — "
            "l'image sera disponible dans le catalogue avec ses indices "
            "NDVI, NDWI, MNDWI calculés automatiquement."
        )
    except Exception as exc:
        return False, f"Impossible de lancer l'agent : {exc}"


# ===========================================================================
# GÉNÉRATION DE LA RÉPONSE FINALE
# ===========================================================================

def generate_response(user_message, chat_history, registry_context,
                      found_scenes, download_triggered, download_success,
                      download_message, groq_client) -> str:
    """Génère la réponse finale en français."""

    scenes_context = ""
    if found_scenes:
        scenes_context = "\n\nIMAGES TROUVÉES :\n"
        for i, s in enumerate(found_scenes, 1):
            cloud    = s.get("cloud_cover_scl")
            date_raw = s.get("content_date", "?")
            try:
                date_fmt = datetime.fromisoformat(date_raw[:10]).strftime("%d/%m/%Y")
            except Exception:
                date_fmt = date_raw[:10] if date_raw else "?"

            scenes_context += f"""
Image {i} :
  - Nom       : {s.get('name','?')[:55]}
  - Satellite : {s.get('collection','?')}
  - Date      : {date_fmt}
  - Nuages    : {f"{cloud:.1f}%" if cloud is not None else "N/A"}
  - Indices   : {s.get('indices_computed','—') or '—'}
  - Dossier   : {s.get('processed_dir','N/A') or 'N/A'}
"""
    elif download_triggered:
        scenes_context = f"""
    IMPORTANT - ACTION EFFECTUÉE :
    L'agent de téléchargement a été lancé automatiquement en arrière-plan.
    Message : {download_message}

    INSTRUCTION : Informe l'utilisateur que le téléchargement a été lancé
    et qu'il doit revenir dans 15 minutes. Ne donne PAS d'instructions
    pour lancer l'agent manuellement car c'est déjà fait automatiquement.
    """
    else:
        scenes_context = "\nAucune image trouvée pour ces critères."

    system = f"""Tu es l'assistant IA de la DGRE (Direction Générale des 
Ressources en Eau de Tunisie), expert en images satellitaires.
Tu réponds TOUJOURS en français, de manière claire et professionnelle.

INTERPRÉTATION DES INDICES (grille de lecture générale uniquement) :
- NDVI > 0.5  → végétation dense
- NDVI 0.2-0.5 → végétation modérée
- NDVI < 0.2  → sol nu / végétation éparse
- NDVI < 0    → eau / surfaces imperméables
- NDWI > 0    → eau libre (barrage, oued, lac)
- MNDWI > 0   → eau zone aride (recommandé Tunisie)
- NDMI > 0.4  → végétation bien hydratée
- NDMI < 0    → stress hydrique

{registry_context}
{scenes_context}

RÈGLE ABSOLUE - NE JAMAIS INVENTER DE DONNÉES :
Tu n'as PAS accès aux valeurs réelles des pixels (pas de moyenne NDVI par
zone, pas de valeur exacte par parcelle, etc.). Tu connais seulement :
le nom/date/satellite/nuages de la scène, et la LISTE des indices qui ont
été calculés (champ "Indices"). Tu n'as PAS les valeurs numériques de ces
indices.
- N'invente JAMAIS de tableau de valeurs par zone (ex: "Centre urbain :
  NDVI 0,12"), de moyenne, de pourcentage de couverture végétale ou de
  chiffre précis que tu n'as pas reçu ci-dessus. C'est une donnée fabriquée
  et c'est interdit, même à titre "d'exemple" ou d'illustration.
- Tu peux UNIQUEMENT rappeler la grille de lecture générale des indices
  (au-dessus), sans l'appliquer à des valeurs chiffrées inventées.
- Si l'utilisateur demande une valeur ou une analyse chiffrée précise que
  tu n'as pas, dis clairement que ce chiffre n'est pas disponible ici et
  qu'il faut consulter la carte d'indice (image) affichée juste au-dessus
  de ta réponse pour une lecture visuelle, sans besoin de SIG.
- Les cartes d'indices et le composite couleur sont déjà affichés
  directement dans le chat (aperçu image) : dis à l'utilisateur qu'il peut
  les voir juste au-dessus, sans avoir à ouvrir de logiciel SIG.
- Un aperçu photo réel (quicklook fourni par l'ESA) est aussi disponible
  pour certaines scènes, y compris pour le radar Sentinel-1 qui n'a pas
  d'indices calculés — mentionne-le si l'utilisateur veut "voir la photo"
  ou "voir à quoi ça ressemble" et qu'aucun indice n'est disponible.

Si des images ont été trouvées : présente-les avec leurs caractéristiques
réelles uniquement (nom, date, satellite, nuages, indices calculés).
Si download_triggered=True : DIS OBLIGATOIREMENT que le téléchargement 
a DÉJÀ été lancé automatiquement en arrière-plan. 
Utilise le passé composé "j'ai lancé" pas le futur "je pourrai lancer".
NE demande PAS à l'utilisateur s'il veut lancer - c'est DÉJÀ fait.

INTERDICTION ABSOLUE — NE JAMAIS INVENTER D'ACTION NI D'INTERFACE :
Ne dis JAMAIS "j'ai converti", "j'ai exporté en PNG" ou toute formulation
au passé décrivant une action que tu n'as pas réellement effectuée. Si des
images sont listées dans IMAGES TROUVÉES, dis simplement qu'elles sont
déjà affichées en PNG juste au-dessus de ta réponse dans le chat (composite
couleur et cartes d'indices), et que les fichiers bruts .tif sont
téléchargeables via les boutons prévus à cet effet.
Le dashboard DGRE n'a PAS de menu "Calcul d'indice" ni de bouton de
sélection d'image pour lancer un calcul manuellement dans le chat. N'invente
JAMAIS ce genre d'étape ou de menu. Si les indices d'une scène ne sont pas
encore disponibles, dis simplement : "Les indices n'ont pas encore été
calculés pour cette scène." sans proposer de marche à suivre inventée.

Termine par une recommandation utile, sans inventer de chiffres ni d'actions."""


    api_messages = [{"role": "system", "content": system}]
    for msg in chat_history[-6:]:
        api_messages.append({"role": msg["role"], "content": msg["content"]})
    api_messages.append({"role": "user", "content": user_message})

    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=api_messages,
            temperature=0.3,
            max_tokens=1024,
        )
        return resp.choices[0].message.content
    except Exception as exc:
        return f"❌ Erreur Groq : {exc}"


# ===========================================================================
# FONCTION PRINCIPALE
# ===========================================================================

def process_chat_message(user_message: str, chat_history: list,
                         auto_download: bool = True) -> tuple:
    """
    Traite un message utilisateur.
    Retourne (response_text, downloadable_files, scenes_media).
    """
    try:
        from groq import Groq
    except ImportError:
        return "❌ Installez groq : `pip install groq`", [], []

    if not GROQ_API_KEY:
        return "❌ GROQ_API_KEY manquant dans `.env`", [], []

    client = Groq(api_key=GROQ_API_KEY)

    # 1. Charger contexte (depuis cache si possible)
    registry_context = get_registry_context()

    # 2. Extraire paramètres
    params = extract_search_params(user_message, client)
    logger.info(f"Paramètres : {params}")

    # 3. Chercher dans le registre
    found_scenes = search_in_registry(params)
    logger.info(f"{len(found_scenes)} image(s) trouvée(s)")

    # 3bis. Calcul des indices à la demande pour les scènes qui n'en ont pas
    # encore, mais dont les données brutes sont déjà sur le disque (aucun
    # nouveau téléchargement nécessaire).
    if found_scenes and Path(DB_PATH).exists():
        reg_for_compute = Registry(db_path=DB_PATH)
        found_scenes = [
            ensure_indices_computed(s, reg_for_compute) for s in found_scenes
        ]
    


    # 4. Si rien trouvé → lancer téléchargement automatiquement
    # CORRECTION : on ne vérifie plus is_download_request
    # → le téléchargement se lance dès qu'une image est manquante
    download_triggered = False
    download_success   = False
    download_message   = ""

    if not found_scenes and auto_download:
        download_triggered = True
        logger.info("Déclenchement téléchargement automatique...")
        download_success, download_message = trigger_download(params)

    # 5. Collecter fichiers .tif téléchargeables + aperçus (composite / indices)
    downloadable_files = []
    scenes_media = []
    for scene in found_scenes:
        processed_dir = scene.get("processed_dir")
        if processed_dir and Path(processed_dir).exists():
            for tif in Path(processed_dir).glob("*.tif"):
                downloadable_files.append({
                    "name":  tif.name,
                    "path":  str(tif),
                    "scene": scene.get("name", "?")[:30],
                    "date":  scene.get("content_date", "?")[:10],
                })

        # Aperçu photo réel (quicklook ESA) — proposé aussi en téléchargement,
        # même quand aucun indice n'a été calculé (cas typique de Sentinel-1)
        quicklook = scene.get("preview_png")
        if quicklook and Path(quicklook).exists():
            downloadable_files.append({
                "name":  f"apercu_{scene.get('name', 'scene')[:30]}.png",
                "path":  quicklook,
                "scene": scene.get("name", "?")[:30],
                "date":  scene.get("content_date", "?")[:10],
            })

        scenes_media.append(build_scene_media(scene))

    # 6. Générer réponse
    response = generate_response(
        user_message=user_message,
        chat_history=chat_history,
        registry_context=registry_context,
        found_scenes=found_scenes,
        download_triggered=download_triggered,
        download_success=download_success,
        download_message=download_message,
        groq_client=client,
    )

    return response, downloadable_files, scenes_media
import shutil
import sqlite3
import re
from pathlib import Path
from datetime import datetime

DB_PATH = "data/registry.sqlite3"

MISPLACED = [
    {
        "old_path":   Path("data/SENTINEL-1/tunis_sentinel1/S1A_IW_GRDH_1SDV_20260622T172034_20260622T172059_065085_083425_E1A2.SAFE"),
        "collection": "SENTINEL-1",
        "watch_name": "tunis_sentinel1",
    },
    {
        "old_path":   Path("data/SENTINEL-2/tunis_sentinel2/S2C_MSIL2A_20260626T101021_N0512_R022_T32SPF_20260626T152309.SAFE"),
        "collection": "SENTINEL-2",
        "watch_name": "tunis_sentinel2",
    },
]


def find_true_safe_dir(safe_dir: Path) -> Path:
    """Repère le vrai contenu si la structure est doublée (.SAFE/.SAFE/...)."""
    inner = safe_dir / safe_dir.name
    if inner.exists() and (inner / "GRANULE").exists():
        return inner
    return safe_dir


def parse_date(name: str):
    m = re.search(r'_(\d{8})T\d{6}_', name)
    if m:
        d = m.group(1)
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}T00:00:00.000Z"
    return None


conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
now = datetime.now().isoformat(timespec="seconds")

for item in MISPLACED:
    old_path = item["old_path"]
    if not old_path.exists():
        print(f"⚠️  Introuvable, ignoré : {old_path}")
        continue

    name = old_path.name
    true_dir = find_true_safe_dir(old_path)

    content_date = parse_date(name)
    year, month = content_date[0:4], content_date[5:7]

    new_dir = Path("data/raw") / item["collection"] / item["watch_name"] / year / month / name
    new_dir.parent.mkdir(parents=True, exist_ok=True)

    if new_dir.exists():
        print(f"⚠️  Existe déjà à destination, ignoré : {new_dir}")
        continue

    print(f"Déplacement : {true_dir}  →  {new_dir}")
    shutil.move(str(true_dir), str(new_dir))

    cur.execute(
        "INSERT INTO downloaded_products "
        "(product_id, name, collection, watch_name, content_date, "
        " downloaded_at, status, raw_dir, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'validated', ?, ?)",
        (name, name, item["collection"], item["watch_name"],
         content_date, now, str(new_dir), now),
    )
    print(f"✅ Inscrite dans le registre : {name}")

conn.commit()
conn.close()

# Nettoyage des dossiers racine devenus vides/obsolètes
for leftover in [Path("data/SENTINEL-1"), Path("data/SENTINEL-2")]:
    if leftover.exists():
        shutil.rmtree(leftover, ignore_errors=True)
        print(f"🗑️  Dossier résiduel supprimé : {leftover}")

print("\nTerminé.")
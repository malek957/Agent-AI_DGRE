import sqlite3
import re
from pathlib import Path
from datetime import datetime

DB_PATH  = "data/registry.sqlite3"
RAW_ROOT = Path("data/raw")


def parse_date_from_name(name: str):
    """Extrait la date depuis le nom du produit (ex: ..._20260902T101559_...)."""
    m = re.search(r'_(\d{8})T\d{6}_', name)
    if m:
        d = m.group(1)
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}T00:00:00.000Z"
    return None


def guess_collection(name: str):
    if name.startswith("S1"):
        return "SENTINEL-1"
    if name.startswith("S2"):
        return "SENTINEL-2"
    return "UNKNOWN"


conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
existing_names = {r[0] for r in cur.execute("SELECT name FROM downloaded_products").fetchall()}

to_insert = []

for collection_dir in RAW_ROOT.iterdir():
    if not collection_dir.is_dir():
        continue
    for watch_dir in collection_dir.iterdir():
        if not watch_dir.is_dir():
            continue
        watch_name = watch_dir.name
        for safe_dir in watch_dir.rglob("*.SAFE"):
            # On ignore le .SAFE imbriqué À L'INTÉRIEUR du dossier .SAFE conteneur
            if safe_dir.parent.name.endswith(".SAFE"):
                continue
            name = safe_dir.name
            if name in existing_names:
                continue
            to_insert.append({
                "product_id":   name,  # identifiant d'origine perdu, on réutilise le nom
                "name":         name,
                "collection":   guess_collection(name),
                "watch_name":   watch_name,
                "content_date": parse_date_from_name(name),
                "raw_dir":      str(safe_dir),
            })

print(f"{len(to_insert)} scène(s) trouvée(s) sur le disque, absente(s) de la base :\n")
for item in to_insert:
    print(f"- {item['name']}  (zone={item['watch_name']}, date={item['content_date']})")

if not to_insert:
    print("Rien à réimporter.")
else:
    confirm = input("\nTaper OUI pour les réinscrire dans le registre (statut 'validated') : ")
    if confirm.strip().upper() == "OUI":
        now = datetime.now().isoformat(timespec="seconds")
        for item in to_insert:
            cur.execute(
                "INSERT INTO downloaded_products "
                "(product_id, name, collection, watch_name, content_date, "
                " downloaded_at, status, raw_dir, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'validated', ?, ?)",
                (item["product_id"], item["name"], item["collection"],
                 item["watch_name"], item["content_date"], now,
                 item["raw_dir"], now),
            )
        conn.commit()
        print(f"\n✅ {len(to_insert)} scène(s) réinscrite(s).")
    else:
        print("Annulé.")

conn.close()
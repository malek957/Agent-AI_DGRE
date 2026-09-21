import sqlite3
import shutil
from pathlib import Path

DB_PATH = "data/registry.sqlite3"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT product_id, name, status, rejection_reason, raw_dir, processed_dir "
    "FROM downloaded_products WHERE status LIKE 'rejected%'"
).fetchall()

if not rows:
    print("Aucune scène rejetée trouvée.")
else:
    print(f"{len(rows)} scène(s) rejetée(s) trouvée(s) :\n")
    for r in rows:
        print(f"- {r['name']}  |  statut : {r['status']}  |  raison : {r['rejection_reason']}")

    confirm = input("\nTaper OUI pour réinitialiser ces scènes (le registre les oubliera "
                     "et l'agent les retéléchargera au prochain lancement) : ")
    if confirm.strip().upper() == "OUI":
        for r in rows:
            # Supprime le fichier brut corrompu s'il existe, pour forcer un téléchargement complet
            if r["raw_dir"] and Path(r["raw_dir"]).exists():
                shutil.rmtree(r["raw_dir"], ignore_errors=True)
                print(f"  Fichier supprimé : {r['raw_dir']}")

        conn.execute("DELETE FROM downloaded_products WHERE status LIKE 'rejected%'")
        conn.commit()
        print(f"\n✅ {len(rows)} scène(s) réinitialisée(s).")
    else:
        print("Annulé, aucune modification effectuée.")

conn.close()
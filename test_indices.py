"""
test_indices.py — Teste le calcul d'indices sur une scène déjà présente
sur le disque, sans retélécharger. Usage : python test_indices.py
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from registry import Registry
from chatbot_engine import ensure_indices_computed

DB_PATH = os.getenv("STORAGE_ROOT", "./data") + "/registry.sqlite3"

# Nom exact du produit à tester (celui qui a échoué dans le log)
PRODUCT_NAME = "S2A_MSIL2A_20260913T100041_N0512_R122_T32SND_20260913T164134.SAFE"

reg = Registry(db_path=DB_PATH)
scenes = reg.get_all_scenes(limit=500)

scene = next((s for s in scenes if s.get("name") == PRODUCT_NAME), None)

if scene is None:
    print(f"❌ Scène introuvable dans le registre : {PRODUCT_NAME}")
    sys.exit(1)

print(f"Scène trouvée : {scene.get('name')}")
print(f"  raw_dir actuel      : {scene.get('raw_dir')}")
print(f"  processed_dir actuel: {scene.get('processed_dir')}")
print(f"  indices_computed    : {scene.get('indices_computed')}")
print()
print("▶ Lancement du calcul d'indices...")

result = ensure_indices_computed(scene, reg)

print()
print("Résultat après calcul :")
print(f"  processed_dir : {result.get('processed_dir')}")
print(f"  indices_computed : {result.get('indices_computed')}")
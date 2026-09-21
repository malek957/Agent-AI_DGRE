"""
test_mpc.py — Teste le connecteur Microsoft Planetary Computer (Landsat 8/9).
Aucun compte requis, aucun token requis.
Exécuter : python test_mpc.py
"""
import logging
from mpc_connector import search_landsat_mpc, _sign_url
import requests

logging.basicConfig(level=logging.INFO, format="%(message)s")

print("=" * 55)
print("TEST CONNECTEUR MICROSOFT PLANETARY COMPUTER")
print("Landsat 8 + Landsat 9 — Tunisie")
print("=" * 55)

# 1. Test connectivité
print("\n1. Test connectivité MPC...")
try:
    r = requests.get(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        timeout=10
    )
    print(f"   ✅ Serveur MPC accessible (code {r.status_code})")
except Exception as e:
    print(f"   ❌ Impossible d'accéder à MPC : {e}")
    exit(1)

# 2. Recherche Landsat 8
print("\n2. Recherche Landsat 8 sur la Tunisie...")
try:
    scenes8 = search_landsat_mpc(
        satellite="landsat8",
        bbox=[7.49, 30.18, 11.60, 37.55],
        date_start="2026-06-01",
        date_end="2026-07-09",
        max_cloud_cover=80,
        max_results=5,
    )
    print(f"   ✅ {len(scenes8)} scène(s) Landsat 8 trouvée(s)")
    for s in scenes8:
        props = s.get("properties", {})
        print(
            f"      → {s['id'][:40]} | "
            f"date={props.get('datetime','?')[:10]} | "
            f"nuages={props.get('eo:cloud_cover','?')}%"
        )
except Exception as e:
    print(f"   ❌ Échec : {e}")

# 3. Recherche Landsat 9
print("\n3. Recherche Landsat 9 sur la Tunisie...")
try:
    scenes9 = search_landsat_mpc(
        satellite="landsat9",
        bbox=[7.49, 30.18, 11.60, 37.55],
        date_start="2026-06-01",
        date_end="2026-07-09",
        max_cloud_cover=80,
        max_results=5,
    )
    print(f"   ✅ {len(scenes9)} scène(s) Landsat 9 trouvée(s)")
    for s in scenes9:
        props = s.get("properties", {})
        print(
            f"      → {s['id'][:40]} | "
            f"date={props.get('datetime','?')[:10]} | "
            f"nuages={props.get('eo:cloud_cover','?')}%"
        )
except Exception as e:
    print(f"   ❌ Échec : {e}")

# 4. Test signature URL
print("\n4. Test signature URL Azure...")
all_scenes = scenes8 + scenes9 if 'scenes8' in dir() and 'scenes9' in dir() else []
if all_scenes:
    first_scene  = all_scenes[0]
    assets       = first_scene.get("assets", {})
    first_asset  = assets.get("red") or assets.get("nir08") or next(iter(assets.values()), None)
    if first_asset:
        url = first_asset.get("href", "")
        try:
            signed = _sign_url(url)
            if signed != url:
                print("   ✅ Signature URL Azure réussie")
            else:
                print("   ⚠️  URL non signée (peut fonctionner quand même)")
        except Exception as e:
            print(f"   ❌ Échec signature : {e}")
else:
    print("   ⏭️  Ignoré (aucune scène trouvée)")

print("\n" + "=" * 55)
print("✅ Test terminé.")
print("   Si des scènes ont été trouvées, le connecteur est prêt.")
print("   Lancez : python agent.py --once pour traiter les images.")
print("=" * 55)

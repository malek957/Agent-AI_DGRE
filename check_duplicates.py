from pathlib import Path

raw_names = {p.name for p in Path("data/raw").rglob("*.SAFE") if not p.parent.name.endswith(".SAFE")}
autre_s1 = {p.name for p in Path("data/SENTINEL-1").rglob("*.SAFE") if not p.parent.name.endswith(".SAFE")} if Path("data/SENTINEL-1").exists() else set()
autre_s2 = {p.name for p in Path("data/SENTINEL-2").rglob("*.SAFE") if not p.parent.name.endswith(".SAFE")} if Path("data/SENTINEL-2").exists() else set()

autre = autre_s1 | autre_s2
doublons = autre & raw_names
uniques  = autre - raw_names

print(f"Scènes dans data/SENTINEL-1|2 (hors raw/) : {len(autre)}")
print(f"  → doublons (déjà dans raw/)            : {len(doublons)}")
print(f"  → uniques (pas encore dans raw/)        : {len(uniques)}")
print()
print("Détail des uniques :")
for n in uniques:
    print(" -", n)
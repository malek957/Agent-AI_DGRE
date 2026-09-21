# Agent de téléchargement automatique d'images satellitaires

Agent Python qui surveille en continu une ou plusieurs zones géographiques et
télécharge automatiquement les nouvelles images Sentinel-1/Sentinel-2 dès
qu'elles sont disponibles sur le Copernicus Data Space Ecosystem (CDSE).

## ⚠️ À propos du "temps réel"

Les satellites Sentinel ne survolent pas une même zone en continu — ils
repassent tous les **5 jours (Sentinel-2)** ou **6 jours (Sentinel-1)**
environ. Cet agent ne peut donc pas vous donner une image "en direct" :
il **vérifie régulièrement** (toutes les 30 min par défaut) si une nouvelle
image est apparue dans le catalogue, et la télécharge dès que c'est le cas.
C'est le "temps réel" réaliste pour ce type de données.

## Installation

```bash
# 1. Cloner/copier ce dossier, puis se placer dedans
cd satellite_agent

# 2. Créer un environnement virtuel (recommandé)
python3 -m venv venv
source venv/bin/activate        # sous Windows: venv\Scripts\activate

# 3. Installer les dépendances
pip install -r requirements.txt

# 4. Créer votre compte (gratuit) sur https://dataspace.copernicus.eu/
#    puis copier le fichier d'exemple et y mettre vos identifiants
cp .env.example .env
nano .env    # remplir CDSE_USERNAME et CDSE_PASSWORD
```

## Configuration des zones à surveiller

Ouvrez `config.py` et modifiez la liste `WATCHES`. Chaque entrée définit :
- `name` : identifiant libre (sert aussi de nom de dossier)
- `collection` : `"SENTINEL-1"` ou `"SENTINEL-2"`
- `product_type` : ex. `"S2MSI2A"` pour Sentinel-2 niveau 2A (corrigé atmosphère)
- `bbox` : la zone géographique `[lon_min, lat_min, lon_max, lat_max]`
  (trouvez vos coordonnées sur https://bboxfinder.com/)
- `max_cloud_cover` : filtre les images trop nuageuses (Sentinel-2 seulement)

## Lancer l'agent

```bash
# Mode continu (boucle infinie, vérifie toutes les POLL_INTERVAL_SECONDS)
python agent.py

# Mode "une seule passe" (pratique pour tester ou pour lancer via cron)
python agent.py --once
```

### Alternative : lancer via cron au lieu d'une boucle infinie

Si vous préférez ne pas garder un processus tournant en permanence, utilisez
`--once` avec une tâche cron :

```bash
# Toutes les 30 minutes
*/30 * * * * cd /chemin/vers/satellite_agent && /chemin/vers/venv/bin/python agent.py --once >> logs/cron.log 2>&1
```

## Structure des fichiers téléchargés

```
data/
├── SENTINEL-2/
│   └── tunis_sentinel2/
│       ├── S2A_MSIL2A_....zip
│       └── ...
├── SENTINEL-1/
│   └── tunis_sentinel1/
│       └── ...
├── registry.sqlite3     ← base qui évite les doublons
└── _state/               ← timestamps de dernière vérification par zone
```

## Structure du code

| Fichier | Rôle |
|---|---|
| `agent.py` | Boucle principale, orchestration |
| `auth.py` | Authentification / gestion du token (renouvelé automatiquement) |
| `catalog.py` | Recherche de produits dans le catalogue OData |
| `downloader.py` | Téléchargement avec retry automatique |
| `registry.py` | Base SQLite locale anti-doublons |
| `config.py` | Vos zones et capteurs à surveiller |

## Limites connues / à adapter

- **Rate limiting** : CDSE limite le nombre de requêtes/téléchargements
  simultanés. Si vous surveillez beaucoup de zones, espacez vos requêtes
  ou augmentez `POLL_INTERVAL_SECONDS`.
- **Espace disque** : une image Sentinel-2 complète pèse ~700 Mo à 1 Go.
  Pensez à purger le dossier `data/` régulièrement ou à brancher un stockage
  externe (S3, etc.) si vous accumulez beaucoup de zones/historique.
- **Extension possible** : pour ajouter Landsat, il faudrait soit utiliser le
  catalogue STAC d'AWS (`landsatlook.usgs.gov` ou `earth-search.aws.element84.com`),
  soit une autre source — dites-moi si vous voulez que je l'ajoute.

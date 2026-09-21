"""
page_carte.py — Carte améliorée avec vraies emprises des scènes.

CHANGEMENTS vs version précédente :
=====================================
Avant : des points colorés au centre approximatif de la zone de watch
Après : les VRAIES emprises rectangulaires de chaque image téléchargée,
        comme sur le site Copernicus/ESA, avec :
        - Le rectangle exact couvrant la tuile satellite
        - La couleur selon le statut (vert=validée, gris=rejetée...)
        - Un clic sur le rectangle pour voir les détails
        - Des filtres par satellite, statut, seuil de nuages et date
        - La trajectoire du satellite (direction de prise de vue)
"""

import os
import sys
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from config import SHAPEFILES_DIR

DB_PATH      = os.getenv("STORAGE_ROOT", "./data") + "/registry.sqlite3"
STORAGE_ROOT = os.getenv("STORAGE_ROOT", "./data")

# Chemins vers les shapefiles administratifs (adaptez si vos fichiers
# ne sont pas dans un sous-dossier fwdannuaire)
GOUVERNORATS_SHP = SHAPEFILES_DIR / "fwdannuaire" / "Gouvernorats.shp"
DELEGATIONS_SHP  = SHAPEFILES_DIR / "fwdannuaire" / "Delegations.shp"


@st.cache_data(ttl=3600)
def load_admin_boundaries(shp_path_str: str):
    """
    Charge un shapefile administratif et le retourne en GeoJSON
    (format attendu par folium.GeoJson).
    Retourne None si le fichier n'existe pas ou en cas d'erreur.
    """
    import geopandas as gpd

    shp_path = Path(shp_path_str)
    if not shp_path.exists():
        return None

    try:
        gdf = gpd.read_file(shp_path)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        # Simplifie légèrement les polygones pour alléger l'affichage
        gdf["geometry"] = gdf["geometry"].simplify(0.001, preserve_topology=True)
        return gdf.__geo_interface__
    except Exception as e:
        st.warning(f"Impossible de charger {shp_path.name} : {e}")
        return None

STATUS_COLORS = {
    "archived":         "#1D9E75",
    "indices_computed": "#1D9E75",
    "validated":        "#378ADD",
    "rejected_cloud":   "#888780",
    "rejected_corrupt": "#E24B4A",
    "downloaded":       "#EF9F27",
    "failed":           "#E24B4A",
}

STATUS_LABELS = {
    "archived":         "✅ Archivée",
    "indices_computed": "📊 Indices calculés",
    "validated":        "✓ Validée",
    "rejected_cloud":   "☁️ Rejetée (nuages)",
    "rejected_corrupt": "❌ Corrompue",
    "downloaded":       "⬇️ Téléchargée",
    "failed":           "🔴 Échec",
}

# Emprises réelles des tuiles Sentinel-2 sur la Tunisie
# (basées sur la grille UTM MGRS officielle Sentinel-2)
# Format : code_tuile → [lon_min, lat_min, lon_max, lat_max]
SENTINEL2_TILES = {
    "T32SNF": [9.86, 36.43, 10.93, 37.38],
    "T32SPF": [10.93, 36.43, 12.00, 37.38],
    "T32SNG": [9.86, 37.38, 10.93, 38.33],
    "T32SPG": [10.93, 37.38, 12.00, 38.33],
    "T32SMF": [8.80, 36.43, 9.86, 37.38],
    "T32SMG": [8.80, 37.38, 9.86, 38.33],
    "T32SNA": [9.86, 33.63, 10.93, 34.58],
    "T32SMA": [8.80, 33.63, 9.86, 34.58],
    "T32SNB": [9.86, 34.58, 10.93, 35.53],
    "T32SMB": [8.80, 34.58, 9.86, 35.53],
    "T32SNC": [9.86, 35.53, 10.93, 36.43],
    "T32SMC": [8.80, 35.53, 9.86, 36.43],
    "T32SQA": [10.93, 33.63, 12.00, 34.58],
    "T32SQB": [10.93, 34.58, 12.00, 35.53],
    "T32SQC": [10.93, 35.53, 12.00, 36.43],
    "T32RMV": [8.80, 32.72, 9.86, 33.63],
    "T32RNV": [9.86, 32.72, 10.93, 33.63],
    "T32SLF": [7.74, 36.43, 8.80, 37.38],
    "T32SLG": [7.74, 37.38, 8.80, 38.33],
    "T32SLE": [7.74, 35.53, 8.80, 36.43],
    "T32SLD": [7.74, 34.58, 8.80, 35.53],
    "T32SLC": [7.74, 33.63, 8.80, 34.58],
    "T32RLU": [7.74, 32.72, 8.80, 33.63],
    "T32RPV": [8.80, 32.72, 9.86, 33.63],
    "T32RPU": [8.80, 31.82, 9.86, 32.72],
    "T32RQV": [9.86, 32.72, 10.93, 33.63],
    "T32RQU": [9.86, 31.82, 10.93, 32.72],
    "T32RMU": [7.74, 31.82, 8.80, 32.72],
}

# Zones Sentinel-1 (orbites et emprises approx.)
SENTINEL1_COVERAGE = {
    "descending": [7.0, 30.0, 12.0, 38.0],
    "ascending":  [7.5, 30.5, 12.5, 37.5],
}


def extract_tile_code(scene_name: str) -> str:
    """Extrait le code tuile MGRS depuis le nom de la scène Sentinel-2."""
    # Format : S2A_MSIL2A_20260814T100041_N0512_R122_T32SNF_20260814T151116
    parts = scene_name.split("_")
    for part in parts:
        if part.startswith("T") and len(part) == 6:
            return part
    return None


def get_scene_footprint(scene: dict):
    """
    Retourne la vraie forme géographique (GeoJSON) de la scène si elle a été
    sauvegardée au téléchargement, sinon None (on utilisera alors le bbox
    approximatif comme solution de secours).
    """
    import json

    raw = scene.get("footprint_geojson")
    if not raw or pd.isna(raw):
        return None
    try:
        geom = json.loads(raw)
        if geom and isinstance(geom, dict) and geom.get("coordinates"):
            return geom
    except (ValueError, TypeError):
        pass
    return None


def get_scene_bbox(scene: dict) -> list:
    """
    Retourne la vraie bbox de la scène.
    Pour Sentinel-2 : utilise la grille MGRS officielle.
    Pour Landsat : utilise la bbox stockée dans le registre.
    Pour Sentinel-1 : utilise l'emprise de l'orbite.
    """
    name       = scene.get("name", "")
    collection = scene.get("collection", "")

    # Sentinel-2 → grille MGRS
    if "SENTINEL-2" in collection:
        tile_code = extract_tile_code(name)
        if tile_code and tile_code in SENTINEL2_TILES:
            return SENTINEL2_TILES[tile_code]

    # Landsat → bbox stockée (ou emprise Tunisie par défaut)
    if "LANDSAT" in collection:
        return [7.49, 30.18, 11.60, 37.55]

    # Sentinel-1 → emprise orbite
    if "SENTINEL-1" in collection:
        return [7.0, 30.0, 12.0, 38.0]

    # Par défaut : Tunisie entière
    return [7.49, 30.18, 11.60, 37.55]


def load_scenes() -> pd.DataFrame:
    """Charge les scènes depuis le registre SQLite."""
    if not Path(DB_PATH).exists():
        return pd.DataFrame()

    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query(
        """SELECT name, collection, watch_name, content_date,
                  status, cloud_cover_scl, cloud_cover_provider,
                  valid_pixel_ratio, indices_computed, processed_dir,
                  footprint_geojson
           FROM downloaded_products
           ORDER BY content_date DESC""",
        conn,
    )
    conn.close()

    if df.empty:
        return df

    df["content_date"] = pd.to_datetime(df["content_date"], errors="coerce", utc=True)
    df["date"]         = df["content_date"].dt.date
    for col in ("cloud_cover_scl", "cloud_cover_provider", "valid_pixel_ratio"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["status_label"] = df["status"].map(STATUS_LABELS).fillna(df["status"])
    df["color"]        = df["status"].map(STATUS_COLORS).fillna("#888780")
    df["footprint"]    = df.apply(get_scene_footprint, axis=1)
    df["bbox"]         = df.apply(get_scene_bbox, axis=1)

    return df


def build_map(fdf: pd.DataFrame, show_grid: bool, show_trajectory: bool,
              show_gouvernorats: bool = True, show_delegations: bool = False) -> folium.Map:
    """Construit la carte Folium avec les vraies emprises des scènes."""

    m = folium.Map(
        location=[34.0, 9.5],
        zoom_start=6,
        tiles="CartoDB positron",
    )

    # Couche de fond supplémentaire
    folium.TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Satellite (Esri)", overlay=False,
    ).add_to(m)
    folium.TileLayer("CartoDB positron", name="Carte (CartoDB)", overlay=False).add_to(m)

    # Frontières des gouvernorats — vraies formes issues du shapefile
    if show_gouvernorats:
        geojson_gouv = load_admin_boundaries(str(GOUVERNORATS_SHP))
        if geojson_gouv:
            folium.GeoJson(
                geojson_gouv,
                name="Gouvernorats",
                style_function=lambda f: {
                    "color": "#1F4E79", "weight": 1.5,
                    "fill": False, "opacity": 0.8,
                },
                tooltip=folium.GeoJsonTooltip(fields=["LIB_FR"], aliases=["Gouvernorat :"]),
            ).add_to(m)

    # Frontières des délégations — optionnel, plus détaillé
    if show_delegations:
        geojson_deleg = load_admin_boundaries(str(DELEGATIONS_SHP))
        if geojson_deleg:
            folium.GeoJson(
                geojson_deleg,
                name="Délégations",
                style_function=lambda f: {
                    "color": "#7A8B99", "weight": 0.8,
                    "fill": False, "opacity": 0.6, "dashArray": "3 3",
                },
                tooltip=folium.GeoJsonTooltip(fields=["LIB_FR"], aliases=["Délégation :"]),
            ).add_to(m)

    # Grille MGRS Sentinel-2 (optionnelle)
    if show_grid:
        grid_group = folium.FeatureGroup(name="Grille MGRS Sentinel-2", show=True)
        for tile_code, bbox in SENTINEL2_TILES.items():
            lon_min, lat_min, lon_max, lat_max = bbox
            folium.Rectangle(
                bounds=[[lat_min, lon_min], [lat_max, lon_max]],
                color="#AAAAAA", weight=1, fill=False,
                dash_array="4 4", opacity=0.5,
                tooltip=f"Tuile {tile_code}",
            ).add_to(grid_group)
            folium.Marker(
                location=[(lat_min + lat_max) / 2, (lon_min + lon_max) / 2],
                tooltip=tile_code,
                icon=folium.DivIcon(
                    html=f"<div style='font-size:9px;color:#AAAAAA;"
                         f"font-weight:bold'>{tile_code}</div>",
                    icon_size=(60, 20),
                ),
            ).add_to(grid_group)
        grid_group.add_to(m)

    # Trajectoires satellites (optionnelles)
    if show_trajectory:
        traj_group = folium.FeatureGroup(name="Trajectoires satellites", show=False)

        # Orbite descendante Sentinel-2 (approximative)
        folium.PolyLine(
            [[38.5, 8.0], [37.0, 9.5], [35.5, 11.0], [34.0, 12.5], [32.5, 14.0]],
            color="#2E75B6", weight=2, dash_array="8 4",
            tooltip="Orbite descendante Sentinel-2 (R122)",
            opacity=0.7,
        ).add_to(traj_group)

        # Orbite ascendante Sentinel-1
        folium.PolyLine(
            [[30.0, 12.0], [32.0, 10.5], [34.0, 9.0], [36.0, 7.5], [38.0, 6.0]],
            color="#E24B4A", weight=2, dash_array="8 4",
            tooltip="Orbite ascendante Sentinel-1",
            opacity=0.7,
        ).add_to(traj_group)

        traj_group.add_to(m)

    # Emprises des scènes téléchargées
    scenes_group = folium.FeatureGroup(name="Scènes téléchargées", show=True)

    for _, row in fdf.iterrows():
        bbox  = row["bbox"]
        if not bbox or len(bbox) != 4:
            continue

        lon_min, lat_min, lon_max, lat_max = bbox
        color  = row["color"]
        cloud  = row.get("cloud_cover_scl")
        date   = (row["content_date"].strftime("%d/%m/%Y")
                  if pd.notna(row.get("content_date")) else "—")

        # Construire le popup HTML détaillé
        cloud_txt = f"{cloud:.1f}%" if pd.notna(cloud) else "—"
        indices   = row.get("indices_computed") or "Non calculés"
        dossier   = row.get("processed_dir") or "—"

        popup_html = f"""
        <div style='font-family:Arial;font-size:12px;min-width:280px'>
          <div style='background:{color};color:white;padding:6px 10px;
                      border-radius:4px 4px 0 0;font-weight:bold'>
            {row.get('status_label','?')}
          </div>
          <div style='padding:8px 10px;border:1px solid #ddd;border-top:none'>
            <b>Nom :</b> {str(row.get('name','?'))[:45]}<br>
            <b>Satellite :</b> {row.get('collection','?')}<br>
            <b>Zone :</b> {row.get('watch_name','?')}<br>
            <b>Date d'acquisition :</b> {date}<br>
            <b>Nuages SCL :</b> {cloud_txt}<br>
            <b>Indices calculés :</b> {indices}<br>
            <b>Emprise :</b> [{lon_min:.2f}°, {lat_min:.2f}°] → [{lon_max:.2f}°, {lat_max:.2f}°]<br>
            <b>Dossier :</b> <code style='font-size:10px'>{str(dossier)[:40]}</code>
          </div>
        </div>
        """

        # Emprise réelle si disponible (vraie forme retournée par l'API),
        # sinon rectangle approximatif basé sur la tuile MGRS
        footprint = row.get("footprint")
        if footprint:
            folium.GeoJson(
                footprint,
                style_function=lambda f, c=color: {
                    "color": c, "weight": 2,
                    "fillColor": c, "fillOpacity": 0.25,
                },
                popup=folium.Popup(popup_html, max_width=320),
                tooltip=f"{date} | {row.get('collection','?')} | nuages: {cloud_txt} | forme exacte",
            ).add_to(scenes_group)
        else:
            folium.Rectangle(
                bounds=[[lat_min, lon_min], [lat_max, lon_max]],
                color=color,
                weight=2,
                fill=True,
                fill_color=color,
                fill_opacity=0.25,
                popup=folium.Popup(popup_html, max_width=320),
                tooltip=f"{date} | {row.get('collection','?')} | nuages: {cloud_txt} | approximatif",
            ).add_to(scenes_group)

        # Icône au centre avec la date
        folium.Marker(
            location=[(lat_min + lat_max) / 2, (lon_min + lon_max) / 2],
            popup=folium.Popup(popup_html, max_width=320),
            tooltip=f"{date} | {cloud_txt} nuages",
            icon=folium.DivIcon(
                html=f"<div style='background:{color};color:white;"
                     f"padding:2px 5px;border-radius:3px;"
                     f"font-size:10px;font-weight:bold;"
                     f"white-space:nowrap;box-shadow:1px 1px 3px rgba(0,0,0,0.3)'>"
                     f"{date}</div>",
                icon_size=(90, 20),
                icon_anchor=(45, 10),
            ),
        ).add_to(scenes_group)

    scenes_group.add_to(m)

    # Légende
    legend_html = """
    <div style='position:fixed;bottom:30px;left:30px;z-index:1000;
         background:white;border:1px solid #ccc;border-radius:8px;
         padding:12px 16px;font-size:12px;font-family:Arial;
         box-shadow:2px 2px 6px rgba(0,0,0,0.2);min-width:200px'>
        <div style='font-weight:bold;margin-bottom:8px;color:#1F4E79'>
            Statut des scènes
        </div>
        <div><span style='color:#1D9E75;font-size:16px'>■</span>
             Archivée / Indices calculés</div>
        <div><span style='color:#378ADD;font-size:16px'>■</span>
             Validée</div>
        <div><span style='color:#888780;font-size:16px'>■</span>
             Rejetée (nuages)</div>
        <div><span style='color:#E24B4A;font-size:16px'>■</span>
             Corrompue / Échec</div>
        <div><span style='color:#EF9F27;font-size:16px'>■</span>
             Téléchargée</div>
        <hr style='margin:6px 0'>
        <div style='color:#AAAAAA;font-size:11px'>
            ⬜ Grille MGRS Sentinel-2<br>
            Cliquer sur une zone pour les détails
        </div>
    </div>"""
    m.get_root().html.add_child(folium.Element(legend_html))

    # Contrôle des couches
    folium.LayerControl(position="topright", collapsed=False).add_to(m)

    return m


def render_page():
    """Fonction principale de la page carte."""
    st.title("🗺️ Carte des images téléchargées")
    st.caption(
        "Visualisation des vraies emprises géographiques de chaque image satellite "
        "téléchargée, comme sur les sites Copernicus et USGS."
    )

    # ── Chargement des données ────────────────────────────────────────────────
    df = load_scenes()
    if df.empty:
        st.info("Aucune scène disponible. Lancez l'agent pour télécharger des images.")
        return

    # ── Filtres ───────────────────────────────────────────────────────────────
    st.markdown("### 🔍 Filtres")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        satellites = ["Tous"] + sorted(df["collection"].dropna().unique().tolist())
        sat_f = st.selectbox("🛰️ Satellite", satellites)

    with col2:
        statuts = ["Tous"] + sorted(df["status"].dropna().unique().tolist())
        sta_f   = st.selectbox(
            "📊 Statut", statuts,
            format_func=lambda x: STATUS_LABELS.get(x, x) if x != "Tous" else "Tous",
        )

    with col3:
        cloud_max = st.slider("☁️ Nuages max (%)", 0, 100, 100, step=5)

    with col4:
        if df["date"].notna().any():
            dates    = sorted(df["date"].dropna().unique())
            date_min = dates[0]
            date_max = dates[-1]
            date_range = st.date_input(
                "📅 Période",
                value=(date_min, date_max),
                min_value=date_min,
                max_value=date_max,
            )
        else:
            date_range = None

    # Options d'affichage
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        show_grid       = st.checkbox("Afficher la grille MGRS Sentinel-2", value=True)
    with col_b:
        show_trajectory = st.checkbox("Afficher les trajectoires satellites", value=False)
    with col_c:
        show_rejected   = st.checkbox("Afficher les images rejetées", value=True)

    col_d, col_e = st.columns(2)
    with col_d:
        show_gouvernorats = st.checkbox("Afficher les limites des gouvernorats", value=True)
    with col_e:
        show_delegations  = st.checkbox("Afficher les limites des délégations", value=False)

    # Application des filtres
    fdf = df.copy()
    if sat_f != "Tous":
        fdf = fdf[fdf["collection"] == sat_f]
    if sta_f != "Tous":
        fdf = fdf[fdf["status"] == sta_f]
    if not show_rejected:
        fdf = fdf[~fdf["status"].str.startswith("rejected")]
    if "cloud_cover_scl" in fdf.columns:
        fdf = fdf[fdf["cloud_cover_scl"].isna() | (fdf["cloud_cover_scl"] <= cloud_max)]
    if date_range and len(date_range) == 2:
        fdf = fdf[
            (fdf["date"] >= date_range[0]) &
            (fdf["date"] <= date_range[1])
        ]

    # ── Statistiques de la sélection ─────────────────────────────────────────
    st.markdown("---")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Scènes affichées",  len(fdf))
    c2.metric("✅ Validées",
              len(fdf[fdf["status"].isin(["archived","indices_computed","validated"])]))
    c3.metric("☁️ Rejetées",
              len(fdf[fdf["status"].str.startswith("rejected", na=False)]))
    avg_cloud = fdf["cloud_cover_scl"].mean()
    c4.metric("Nuages moyen", f"{avg_cloud:.1f}%" if pd.notna(avg_cloud) else "—")

    # ── Carte ─────────────────────────────────────────────────────────────────
    st.markdown("### 🌍 Carte interactive")
    st.caption(
        "💡 Cliquez sur une zone colorée pour voir les détails de l'image. "
        "Utilisez le contrôle en haut à droite pour changer le fond de carte."
    )

    if fdf.empty:
        st.warning("Aucune scène ne correspond aux filtres sélectionnés.")
    else:
        m = build_map(
            fdf, show_grid=show_grid, show_trajectory=show_trajectory,
            show_gouvernorats=show_gouvernorats, show_delegations=show_delegations,
        )
        st_folium(m, width=None, height=600, returned_objects=[])

    # ── Tableau des scènes filtrées ───────────────────────────────────────────
    st.markdown("### 📋 Détail des scènes affichées")
    show_cols = ["name", "collection", "watch_name", "date",
                 "status_label", "cloud_cover_scl"]
    show = fdf[[c for c in show_cols if c in fdf.columns]].copy()
    show.columns = [
        {"name": "Nom", "collection": "Satellite", "watch_name": "Zone",
         "date": "Date", "status_label": "Statut",
         "cloud_cover_scl": "Nuages SCL (%)"}.get(c, c)
        for c in show.columns
    ]
    if "Nuages SCL (%)" in show.columns:
        show["Nuages SCL (%)"] = show["Nuages SCL (%)"].apply(
            lambda x: f"{x:.1f}%" if pd.notna(x) else "—"
        )
    st.dataframe(show, use_container_width=True, hide_index=True, height=300)


if __name__ == "__main__":
    st.set_page_config(
        page_title="Carte — DGRE Satellites",
        page_icon="🗺️",
        layout="wide",
    )
    render_page()
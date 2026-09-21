"""
dashboard.py — Tableau de bord Streamlit v4 avec chatbot intelligent.

NOUVEAUTÉS v4 :
================
- Chatbot avec téléchargement automatique si image manquante
- Boutons de téléchargement direct des fichiers .tif
- Mémoire de session complète
- Réponses contextuelles avec interprétation des indices

LANCEMENT :
    streamlit run dashboard.py
"""

import os
import sys
import subprocess
import hashlib
from datetime import datetime
from pathlib import Path

import streamlit as st
import pandas as pd
import plotly.express as px
from dotenv import load_dotenv
from page_carte import render_page as render_carte

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))
from registry import Registry
from config import WATCHES, MPC_WATCHES, INDICES_TO_COMPUTE, DEFAULT_CLOUD_THRESHOLD
from chatbot_engine import process_chat_message
from activity_log import log_action, get_logs
# ── Configuration ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="DGRE — Images Satellitaires",
    page_icon="🛰️",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH        = os.getenv("STORAGE_ROOT", "./data") + "/registry.sqlite3"
STORAGE_ROOT   = os.getenv("STORAGE_ROOT", "./data")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")

# ── Comptes et rôles ─────────────────────────────────────────────────────────
USERS = {
    os.getenv("DASHBOARD_USER", "admin"): {
        "password": os.getenv("DASHBOARD_PASSWORD", "dgre2026"),
        "role": "admin",
    },
    os.getenv("DASHBOARD_USER2", "technicien"): {
        "password": os.getenv("DASHBOARD_PASSWORD2", "dgre2026user"),
        "role": "user",
    },
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

STATUS_COLORS = {
    "archived":         "#1D9E75",
    "indices_computed": "#1D9E75",
    "validated":        "#378ADD",
    "rejected_cloud":   "#888780",
    "rejected_corrupt": "#E24B4A",
    "downloaded":       "#EF9F27",
    "failed":           "#E24B4A",
}

STATUS_COLORS = {
    "archived":         "#1D9E75",
    "indices_computed": "#1D9E75",
    "validated":        "#378ADD",
    "rejected_cloud":   "#888780",
    "rejected_corrupt": "#E24B4A",
    "downloaded":       "#EF9F27",
    "failed":           "#E24B4A",
}


def afficher_signature():
    st.markdown(
        """
        <style>
        .signature-coin {
            position: fixed;
            top: 8px;
            right: 15px;
            font-size: 12px;
            color: #888888;
            z-index: 9999;
        }
        </style>
        <div class="signature-coin">
            Développé par Malek Elhabib
        </div>
        """,
        unsafe_allow_html=True,
    )

# Seuil de nuages propre à chaque zone (repris de WATCHES / MPC_WATCHES),
# utilisé uniquement pour l'AFFICHAGE. Le pipeline ne rejette plus pour les
# nuages, mais on veut quand même distinguer visuellement une image
# "Critique" (nuages au-dessus du seuil habituel, mais conservée et
# exploitable) d'une image "Validée" sans réserve particulière.
CLOUD_THRESHOLDS_BY_WATCH = {
    w["name"]: w.get("cloud_threshold", DEFAULT_CLOUD_THRESHOLD) for w in WATCHES
}
CLOUD_THRESHOLDS_BY_WATCH.update({
    w["name"]: w.get("cloud_threshold", DEFAULT_CLOUD_THRESHOLD) for w in MPC_WATCHES
})


def compute_status_label(row) -> str:
    status = row.get("status")
    cloud  = row.get("cloud_cover_scl")
    watch  = row.get("watch_name")

    if status in ("validated", "indices_computed", "archived") and pd.notna(cloud):
        threshold = CLOUD_THRESHOLDS_BY_WATCH.get(watch, DEFAULT_CLOUD_THRESHOLD)
        if cloud > threshold:
            return "🟠 Critique"

    return STATUS_LABELS.get(status, status)


# ══════════════════════════════════════════════════════════════════════════════
# AUTHENTIFICATION
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# AUTHENTIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def check_login() -> bool:
    if st.session_state.get("authenticated"):
        return True
    col = st.columns([1, 1.2, 1])[1]
    with col:
        st.markdown(
            "<br><br>"
            "<div style='text-align:center;font-size:2rem'>🛰️</div>"
            "<div style='text-align:center;font-weight:600;font-size:1.1rem'>"
            "DGRE — Agent IA Satellitaire</div>"
            "<div style='text-align:center;color:gray;font-size:0.85rem;"
            "margin-bottom:1.5rem'>Sous-direction de l'eau de surface</div>",
            unsafe_allow_html=True,
        )
        with st.form("login"):
            user = st.text_input("Identifiant", placeholder="admin")
            pwd  = st.text_input("Mot de passe", type="password")
            ok   = st.form_submit_button("Se connecter", use_container_width=True)
        if ok:
            account = USERS.get(user)
            if account and pwd == account["password"]:
                st.session_state["authenticated"] = True
                st.session_state["username"]      = user
                st.session_state["role"]          = account["role"]
                log_action(DB_PATH, user, account["role"], "login")
                st.rerun()
            else:
                st.error("Identifiant ou mot de passe incorrect.")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# CHARGEMENT DES DONNÉES
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=30)
def load_data():
    if not Path(DB_PATH).exists():
        return pd.DataFrame()
    reg    = Registry(db_path=DB_PATH)
    scenes = reg.get_all_scenes(limit=5000)
    if not scenes:
        return pd.DataFrame()
    df = pd.DataFrame(scenes)
    if "content_date" in df.columns:
        df["content_date"] = pd.to_datetime(df["content_date"], errors="coerce", utc=True)
        df["date"]         = df["content_date"].dt.date
    for col in ("cloud_cover_scl", "valid_pixel_ratio", "cloud_cover_provider"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["status_label"] = df.apply(compute_status_label, axis=1)
    return df


@st.cache_data(ttl=30)
def load_kpis():
    if not Path(DB_PATH).exists():
        return {}
    return Registry(db_path=DB_PATH).get_stats()


@st.cache_data(ttl=30)
def load_runs():
    if not Path(DB_PATH).exists():
        return []
    return Registry(db_path=DB_PATH).get_recent_runs(limit=10)


def load_storage():
    root = Path(STORAGE_ROOT)
    result = {}
    for folder in ("raw", "processed", "quarantine"):
        p = root / folder
        if p.exists():
            files = list(p.rglob("*"))
            size  = sum(f.stat().st_size for f in files if f.is_file())
            result[folder] = {
                "files":   sum(1 for f in files if f.is_file()),
                "size_mb": round(size / 1024 / 1024, 1),
            }
        else:
            result[folder] = {"files": 0, "size_mb": 0}
    return result


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 1 — ACCUEIL
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# SECTIONS RÉUTILISABLES (catalogue + lancement agent, utilisées sur plusieurs pages)
# ══════════════════════════════════════════════════════════════════════════════

def render_catalogue_table(df, key_prefix: str):
    """
    Filtres + tableau du catalogue + export CSV.
    Réutilisé par la page Catalogue ET par la page d'Accueil.
    key_prefix évite les collisions de clés de widgets entre les deux pages.
    """
    if df.empty:
        st.info("Aucune scène.")
        return df  # fdf vide

    col1, col2, col3 = st.columns(3)
    with col1:
        sat_f = st.selectbox("Satellite",
                             ["Tous"] + sorted(df["collection"].dropna().unique().tolist()),
                             key=f"{key_prefix}_sat")
    with col2:
        sta_f = st.selectbox("Statut",
                             ["Tous"] + sorted(df["status"].dropna().unique().tolist()),
                             format_func=lambda x: STATUS_LABELS.get(x, x)
                             if x != "Tous" else "Tous",
                             key=f"{key_prefix}_statut")
    with col3:
        cloud_max = st.slider("Nuages max (%)", 0, 100, 100, key=f"{key_prefix}_nuages")

    fdf = df.copy()
    if sat_f != "Tous":
        fdf = fdf[fdf["collection"] == sat_f]
    if sta_f != "Tous":
        fdf = fdf[fdf["status"] == sta_f]
    if "cloud_cover_scl" in fdf.columns:
        fdf = fdf[fdf["cloud_cover_scl"].isna() | (fdf["cloud_cover_scl"] <= cloud_max)]

    st.caption(f"**{len(fdf)}** scène(s)")
    display = ["name", "collection", "watch_name", "content_date",
               "status_label", "cloud_cover_scl", "indices_computed"]
    show = fdf[[c for c in display if c in fdf.columns]].copy()
    if "content_date" in show.columns:
        show["content_date"] = show["content_date"].apply(
            lambda x: x.strftime("%d/%m/%Y") if pd.notna(x) else "—")
    if "cloud_cover_scl" in show.columns:
        show["cloud_cover_scl"] = show["cloud_cover_scl"].apply(
            lambda x: f"{x:.1f}%" if pd.notna(x) else "—")
    show.columns = [{"name":"Nom","collection":"Satellite","watch_name":"Zone",
                     "content_date":"Date","status_label":"Statut",
                     "cloud_cover_scl":"Nuages SCL","indices_computed":"Indices"
                     }.get(c,c) for c in show.columns]
    st.dataframe(show, use_container_width=True, hide_index=True, height=380)
    csv = show.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Exporter CSV", data=csv,
                       file_name=f"dgre_{datetime.now():%Y%m%d}.csv",
                       mime="text/csv", key=f"{key_prefix}_csv")
    return fdf


def render_launch_section(key_prefix: str, compact: bool = False):
    if st.session_state.get("role") != "admin":
        st.info("🔒 Seul un administrateur peut lancer l'agent (accès en consultation seule).")
        return

    runs = load_runs()
    if runs:
        last = runs[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Dernière exécution", str(last.get("started_at","—"))[:16])
        c2.metric("Statut", last.get("status","—"))
        c3.metric("Exécutions totales", len(runs))
        st.markdown("---")

    with st.form(f"{key_prefix}_launch_form"):
        mode = st.radio("Mode", [
            "--once (passe complète Sentinel + Landsat)",
            "--once --dry-run (simulation sans télécharger)",
        ], key=f"{key_prefix}_mode")
        submit = st.form_submit_button(
            "🚀 Lancer maintenant", use_container_width=True, type="primary"
        )

    if submit:
        log_action(DB_PATH, st.session_state.get("username", "?"),
                   st.session_state.get("role", "?"), "lancement_agent", mode)

        cmd = [sys.executable, "agent.py", "--once"]
        if "--dry-run" in mode:
            cmd.append("--dry-run")

        log_area  = st.empty()
        status_ph = st.empty()
        log_lines = []
        status_ph.info("⏳ Agent en cours d'exécution (Sentinel + Landsat)...")

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
                cwd=str(Path(__file__).parent),
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    log_lines.append(line)
                    log_area.code("\n".join(log_lines[-40:]), language="")
            proc.wait()
            if proc.returncode == 0:
                status_ph.success("✅ Exécution terminée.")
            else:
                status_ph.error(f"❌ Code retour : {proc.returncode}")
        except Exception as exc:
            status_ph.error(f"Erreur : {exc}")

        st.cache_data.clear()

    if not compact:
        st.markdown("---")
        st.markdown("### 🕓 Historique")
        if runs:
            runs_df = pd.DataFrame(runs)
            cols    = ["started_at","finished_at","triggered_by","status",
                       "scenes_found","validated","rejected"]
            show    = runs_df[[c for c in cols if c in runs_df.columns]]
            st.dataframe(show, use_container_width=True, hide_index=True)
        else:
            st.info("Aucune exécution enregistrée.")


def page_accueil(df, kpis):
    st.title("🛰️ Tableau de bord — Images Satellitaires DGRE")
    st.caption(
        f"Direction Générale des Ressources en Eau · "
        f"Connecté : **{st.session_state.get('username','?')}**"
    )
    nb_critique = int((df["status_label"] == "🟠 Critique").sum()) if not df.empty else 0
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total scènes",   kpis.get("total", 0))
    c2.metric("✅ Validées",    kpis.get("validated", 0))
    c3.metric("🟠 Critique",    nb_critique)
    c4.metric("📊 Avec indices", kpis.get("with_indices", 0))
    c5.metric("Taux de rejet",  f"{kpis.get('rejection_rate', 0)}%")

    if df.empty:
        st.info("Aucune donnée. Lancez l'agent via ⚡ Lancer l'agent.")
        return

    st.markdown("---")
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("##### Répartition par statut")
        counts = df["status_label"].value_counts().reset_index()
        counts.columns = ["Statut", "Nombre"]
        colors = [STATUS_COLORS.get(s, "#888")
                  for s in df["status"].value_counts().index.tolist()]
        fig = px.pie(counts, names="Statut", values="Nombre",
                     hole=0.45, color_discrete_sequence=colors)
        fig.update_layout(height=260, margin=dict(t=10, b=10, l=10, r=10),
                          showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.markdown("##### Par satellite en volume")
        sat = df.groupby(["collection", "status_label"]).size().reset_index(name="n")
        fig = px.bar(sat, x="collection", y="n", color="status_label",
                     labels={"collection": "Satellite", "n": "Images",
                             "status_label": "Statut"})
        fig.update_layout(height=260, margin=dict(t=10, b=10, l=0, r=0))
        st.plotly_chart(fig, use_container_width=True)

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("##### Évolution temporelle")
        if "date" in df.columns and df["date"].notna().any():
            time_df = (df.dropna(subset=["date"])
                       .groupby(["date", "status_label"])
                       .size().reset_index(name="n"))
            fig = px.bar(time_df, x="date", y="n", color="status_label",
                         labels={"date": "Date", "n": "Images"})
            fig.update_layout(height=240, showlegend=False,
                              margin=dict(t=10, b=10, l=0, r=0))
            st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.markdown("##### Distribution couverture nuageuse (SCL)")
        cloud = df.dropna(subset=["cloud_cover_scl"])
        if not cloud.empty:
            fig = px.histogram(cloud, x="cloud_cover_scl", nbins=20,
                               color_discrete_sequence=["#185FA5"])
            fig.add_vline(x=cloud["cloud_cover_scl"].mean(),
                          line_dash="dash", line_color="#E24B4A",
                          annotation_text=f"Moy. {cloud['cloud_cover_scl'].mean():.1f}%")
            fig.update_layout(height=240, margin=dict(t=10, b=10, l=0, r=0))
            st.plotly_chart(fig, use_container_width=True)

    st.markdown("### 💾 Stockage")
    usage = load_storage()
    c1, c2, c3 = st.columns(3)
    icons = {"raw": "📦", "processed": "📊", "quarantine": "🗄️"}
    for col, (name, info) in zip((c1, c2, c3), usage.items()):
        col.metric(f"{icons[name]} {name.capitalize()}",
                   f"{info['size_mb']} Mo", f"{info['files']} fichiers")

    st.markdown("---")
    left, right = st.columns([2, 1])

    with left:
        st.markdown("### 🗂️ Catalogue des scènes")
        fdf = render_catalogue_table(df, key_prefix="accueil")

        st.markdown("---")
        st.markdown("### 🔍 Détail + Télécharger les indices")
        names = fdf["name"].tolist() if not fdf.empty else []
        if names:
            sel = st.selectbox("Choisir une scène", names[:100], key="accueil_scene_sel")
            row = fdf[fdf["name"] == sel].iloc[0]
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(f"**Satellite** : {row.get('collection','—')}")
                st.markdown(f"**Zone** : {row.get('watch_name','—')}")
                date = (row['content_date'].strftime('%d/%m/%Y')
                        if pd.notna(row.get('content_date')) else '—')
                st.markdown(f"**Date** : {date}")
                st.markdown(f"**Statut** : {STATUS_LABELS.get(row.get('status',''),'?')}")
            with c2:
                scl = row.get("cloud_cover_scl")
                vr  = row.get("valid_pixel_ratio")
                st.markdown(f"**Nuages SCL** : {f'{scl:.1f}%' if pd.notna(scl) else '—'}")
                st.markdown(f"**Pixels valides** : {f'{vr:.0%}' if pd.notna(vr) else '—'}")
                st.markdown(f"**Indices** : {row.get('indices_computed','—') or '—'}")

            # Boutons de téléchargement des fichiers .tif
            processed = row.get("processed_dir")
            if processed and Path(str(processed)).exists():
                tifs = list(Path(str(processed)).glob("*.tif"))
                if tifs:
                    st.markdown("**📥 Télécharger les indices GeoTIFF :**")
                    dl_cols = st.columns(min(len(tifs), 3))
                    for dcol, tif in zip(dl_cols, tifs):
                        with dcol:
                            with open(tif, "rb") as f:
                                dcol.download_button(
                                    label=f"⬇️ {tif.stem}",
                                    data=f.read(),
                                    file_name=tif.name,
                                    mime="image/tiff",
                                    key=f"accueil_dl_{tif.name}",
                                )

    with right:
        st.markdown("### ⚡ Lancer l'agent")
        render_launch_section(key_prefix="accueil", compact=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 2 — CARTE
# ══════════════════════════════════════════════════════════════════════════════

def page_carte(df):
    render_carte()


# ══════════════════════════════════════════════════════════════════════════════


def page_indices(df):
    st.title("📊 Indices spectraux")
    idx_df = df[df["indices_computed"].notna() & (df["indices_computed"] != "")].copy()

    if idx_df.empty:
        st.info("Aucune scène avec indices calculés pour l'instant.")
        st.markdown("### Indices configurés")
        desc = {
            "NDVI":  "Végétation — (NIR-RED)/(NIR+RED)",
            "NDWI":  "Plans d'eau — (GREEN-NIR)/(GREEN+NIR)",
            "MNDWI": "Eau zone aride — (GREEN-SWIR1)/(GREEN+SWIR1)",
            "NDMI":  "Humidité végétation — (NIR-SWIR1)/(NIR+SWIR1)",
            "SAVI":  "Végétation sol nu — NDVI corrigé L=0.5",
            "WDVI":  "Végétation pondérée — NIR - g×RED",
        }
        for idx in INDICES_TO_COMPUTE:
            st.markdown(f"- **{idx}** : {desc.get(idx,'')}")
        return

    st.success(f"**{len(idx_df)}** scène(s) avec indices calculés")
    show = idx_df[["name","collection","content_date","indices_computed","cloud_cover_scl"]].copy()
    if "content_date" in show.columns:
        show["content_date"] = show["content_date"].apply(
            lambda x: x.strftime("%d/%m/%Y") if pd.notna(x) else "—")
    if "cloud_cover_scl" in show.columns:
        show["cloud_cover_scl"] = show["cloud_cover_scl"].apply(
            lambda x: f"{x:.1f}%" if pd.notna(x) else "—")
    show.columns = ["Scène","Satellite","Date","Indices","Nuages"]
    st.dataframe(show, use_container_width=True, hide_index=True)

    comp = df.dropna(subset=["cloud_cover_scl","cloud_cover_provider"])
    if not comp.empty:
        st.markdown("### Nuages SCL vs Nuages fournisseur")
        fig = px.scatter(comp, x="cloud_cover_provider", y="cloud_cover_scl",
                         color="collection",
                         labels={"cloud_cover_provider": "Nuages Copernicus (%)",
                                 "cloud_cover_scl": "Nuages SCL recalculés (%)"})
        fig.add_shape(type="line", x0=0, y0=0, x1=100, y1=100,
                      line=dict(dash="dash", color="gray"))
        fig.update_layout(height=380)
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Points au-dessus de y=x : zone plus nuageuse que la tuile entière.")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 5 — CHATBOT IA INTELLIGENT
# ══════════════════════════════════════════════════════════════════════════════

def render_scenes_media(scenes_media: list):
    """
    Affiche, pour chaque scène : sa description, son composite couleur,
    sa/ses carte(s) d'indice (NDVI/NDWI/... quel que soit l'indice calculé),
    et surtout les boutons pour télécharger le RÉSULTAT au format PNG et/ou
    PDF — c'est ce livrable que l'encadrante veut voir, sans SIG.
    """
    for media in scenes_media:
        if not media.get("previews"):
            continue
        st.caption(media.get("description", ""))
        previews = media["previews"]
        cols = st.columns(len(previews))
        for col, prev in zip(cols, previews):
            with col:
                st.image(prev["png_path"], caption=prev["label"], use_container_width=True)

        # Téléchargement du résultat — PNG (planche contact) et/ou PDF (rapport)
        report_png = media.get("report_png")
        report_pdf = media.get("report_pdf")
        if report_png or report_pdf:
            st.markdown("**📄 Télécharger le résultat :**")
            dl_cols = st.columns(2)
            if report_png and Path(report_png).exists():
                with open(report_png, "rb") as f:
                    dl_cols[0].download_button(
                        label="🖼️ Télécharger en PNG",
                        data=f.read(),
                        file_name=Path(report_png).name,
                        mime="image/png",
                        key=f"report_png_{report_png}",
                        use_container_width=True,
                    )
            if report_pdf and Path(report_pdf).exists():
                with open(report_pdf, "rb") as f:
                    dl_cols[1].download_button(
                        label="📄 Télécharger en PDF",
                        data=f.read(),
                        file_name=Path(report_pdf).name,
                        mime="application/pdf",
                        key=f"report_pdf_{report_pdf}",
                        use_container_width=True,
                    )
        st.markdown("---")


def page_chatbot():
    st.title("🤖 Assistant IA — Images Satellitaires")
    st.caption(
        "Posez vos questions en français. L'assistant cherche dans le catalogue "
        "et **télécharge automatiquement** si l'image n'est pas disponible."
    )

    if not GROQ_API_KEY:
        st.error("⚠️ GROQ_API_KEY manquant dans `.env`")
        st.stop()

    # Option téléchargement automatique
    if "auto_download" not in st.session_state:
        st.session_state["auto_download"] = True

    auto_dl = st.toggle(
        "🔄 Téléchargement automatique si image non disponible",
        value=st.session_state["auto_download"],
        key="auto_download",
    )
    help="Si activé, l'agent télécharge automatiquement l'image demandée "
    "si elle n'est pas dans le catalogue.",


    st.markdown("---")

    # Initialisation mémoire de session
    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []
    if "downloadable_files" not in st.session_state:
        st.session_state["downloadable_files"] = []
    if "scenes_media" not in st.session_state:
        st.session_state["scenes_media"] = []

    # Message de bienvenue
    if not st.session_state["chat_history"]:
        welcome = (
            "Bonjour ! Je suis votre assistant IA pour les images satellitaires "
            "de la DGRE. 🛰️\n\n"
            "Je peux :\n"
            "- 🔍 **Rechercher** des images dans le catalogue\n"
            "- ⬇️ **Télécharger automatiquement** les images manquantes\n"
            "- 📊 **Interpréter** les indices NDVI, NDWI, MNDWI, NDMI, SAVI\n"
            "- 💧 **Analyser** les ressources en eau de surface\n\n"
            "**Exemples de questions :**\n"
            "- *\"Je veux une image de Tunis du 17/08/2026 avec moins de 10% de nuages\"*\n"
            "- *\"Quelle est la meilleure image disponible du barrage Sidi Salem ?\"*\n"
            "- *\"Télécharge une image Sentinel-2 de la Tunisie de cette semaine\"*\n\n"
            "Que puis-je faire pour vous ?"
        )
        st.session_state["chat_history"].append({
            "role": "assistant", "content": welcome
        })

    # Affichage de l'historique
    for msg in st.session_state["chat_history"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Boutons de téléchargement des fichiers disponibles
    if st.session_state["downloadable_files"]:
        st.markdown("### 📥 Fichiers disponibles au téléchargement")
        cols = st.columns(min(len(st.session_state["downloadable_files"]), 4))
        for col, file_info in zip(cols, st.session_state["downloadable_files"]):
            with col:
                try:
                    with open(file_info["path"], "rb") as f:
                        col.download_button(
                            label=f"⬇️ {file_info['name']}",
                            data=f.read(),
                            file_name=file_info["name"],
                            mime="image/tiff",
                            key=f"chat_dl_{file_info['path']}",
                            help=f"Scène : {file_info['scene']} | Date : {file_info['date']}",
                        )
                except Exception:
                    col.caption(f"⚠️ {file_info['name']} inaccessible")

    # Aperçus visuels (composite couleur + cartes d'indices) des dernières scènes
    if st.session_state["scenes_media"]:
        st.markdown("### 🖼️ Aperçus des scènes trouvées")
        render_scenes_media(st.session_state["scenes_media"])

    # Suggestions rapides
    st.markdown("**💡 Questions rapides :**")
    suggestions = [
        ("🗓️ Dernières images",    "Quelles sont les 3 dernières images validées disponibles ?"),
        ("☁️ Moins de nuages",     "Quelle est la scène avec le moins de nuages dans le catalogue ?"),
        ("💧 Eau libre",           "Montre-moi les images avec de l'eau libre détectée (NDWI élevé)"),
        ("🌿 Végétation",          "Quelles scènes montrent la meilleure végétation (NDVI élevé) ?"),
        ("📍 Sidi Salem",          "Y a-t-il des images récentes du barrage Sidi Salem ?"),
        ("📈 Résumé catalogue",    "Donne-moi un résumé complet du catalogue d'images"),
    ]
    cols = st.columns(3)
    for i, (label, question) in enumerate(suggestions):
        with cols[i % 3]:
            if st.button(label, key=f"sugg_{i}", use_container_width=True):
                st.session_state["pending_q"] = question
                st.rerun()

    st.markdown("---")

    # Zone de saisie
    user_input = st.chat_input(
        "Posez votre question (ex: je veux une image de Tunis du 17/08/2026)..."
    )

    question = None
    if "pending_q" in st.session_state:
        question = st.session_state.pop("pending_q")
    elif user_input:
        question = user_input

    if question:
        log_action(DB_PATH, st.session_state.get("username", "?"),
                   st.session_state.get("role", "?"), "question_chatbot", question)
        # Ajouter à l'historique
        st.session_state["chat_history"].append({
            "role": "user", "content": question
        })
        with st.chat_message("user"):
            st.markdown(question)

        # Traitement par le moteur chatbot
        with st.chat_message("assistant"):
            with st.spinner("🔍 Recherche dans le catalogue..."):
                response, downloadable_files, scenes_media = process_chat_message(
                    user_message=question,
                    chat_history=st.session_state["chat_history"][:-1],
                    auto_download=auto_dl,
                )

            st.markdown(response)

            # Afficher les aperçus (composite couleur + carte d'indice)
            if scenes_media:
                render_scenes_media(scenes_media)

            # Afficher les boutons de téléchargement si fichiers disponibles
            if downloadable_files:
                st.markdown("**📥 Fichiers disponibles :**")
                cols = st.columns(min(len(downloadable_files), 4))
                for col, fi in zip(cols, downloadable_files):
                    with col:
                        try:
                            with open(fi["path"], "rb") as f:
                                col.download_button(
                                    label=f"⬇️ {fi['name']}",
                                    data=f.read(),
                                    file_name=fi["name"],
                                    mime="image/tiff",
                                    key=f"resp_dl_{fi['path']}",
                                    help=f"Date : {fi['date']}",
                                )
                        except Exception:
                            col.caption(f"⚠️ {fi['name']}")

        # Sauvegarder
        st.session_state["chat_history"].append({
            "role": "assistant", "content": response
        })
        st.session_state["downloadable_files"] = downloadable_files
        st.session_state["scenes_media"]       = scenes_media

        # Rafraîchir le cache des données
        st.cache_data.clear()

    # Réinitialiser
    st.markdown("---")
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🗑️ Nouvelle conversation", use_container_width=True):
            st.session_state["chat_history"]       = []
            st.session_state["downloadable_files"] = []
            st.session_state["scenes_media"]       = []
            st.rerun()
    with col1:
        st.caption(
            f"Mémoire : {len(st.session_state['chat_history'])} messages dans cette session · "
            f"Mise à jour : {datetime.now():%H:%M:%S}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 6 — PARAMÈTRES
# ══════════════════════════════════════════════════════════════════════════════

def page_droits():
    st.title("🔐 Droits d'accès & journal d'activité")

    if st.session_state.get("role") != "admin":
        st.warning("Cette page est réservée aux administrateurs.")
        return

    st.caption("Traçabilité des connexions et des actions effectuées sur la plateforme.")

    logs = get_logs(DB_PATH, limit=500)
    if not logs:
        st.info("Aucune activité enregistrée pour l'instant.")
        return

    logs_df = pd.DataFrame(logs)
    logs_df["timestamp"] = pd.to_datetime(logs_df["timestamp"])

    c1, c2, c3 = st.columns(3)
    c1.metric("Connexions totales", int((logs_df["action"] == "login").sum()))
    c2.metric("Lancements agent", int((logs_df["action"] == "lancement_agent").sum()))
    c3.metric("Utilisateurs actifs", logs_df["username"].nunique())

    col1, col2 = st.columns(2)
    with col1:
        user_f = st.selectbox("Filtrer par utilisateur",
                               ["Tous"] + sorted(logs_df["username"].unique().tolist()))
    with col2:
        action_f = st.selectbox("Filtrer par action",
                                 ["Toutes"] + sorted(logs_df["action"].unique().tolist()))

    fdf = logs_df.copy()
    if user_f != "Tous":
        fdf = fdf[fdf["username"] == user_f]
    if action_f != "Toutes":
        fdf = fdf[fdf["action"] == action_f]

    st.dataframe(
        fdf.rename(columns={
            "username": "Utilisateur", "role": "Rôle", "action": "Action",
            "details": "Détails", "timestamp": "Date / Heure",
        }),
        use_container_width=True, hide_index=True, height=420,
    )

    st.markdown("### 👥 Comptes existants")
    users_show = pd.DataFrame([
        {"Identifiant": u, "Rôle": v["role"]} for u, v in USERS.items()
    ])
    st.dataframe(users_show, use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# NAVIGATION
# ══════════════════════════════════════════════════════════════════════════════

def sidebar_nav(kpis: dict) -> str:
    role = st.session_state.get("role", "user")
    NAV_OPTIONS = ["🏠 Accueil", "🗺️ Carte", "🗂️ Catalogue",
                   "📊 Indices", "🤖 Assistant IA", "⚡ Lancer l'agent"]
    if role == "admin":
        NAV_OPTIONS.append("🔐 Droits d'accès")

    MERGED_INTO_ACCUEIL = {"🗂️ Catalogue", "⚡ Lancer l'agent"}

    if "nav_page" not in st.session_state or st.session_state["nav_page"] not in NAV_OPTIONS:
        st.session_state["nav_page"] = "🏠 Accueil"

    def _on_nav_change():
        selected = st.session_state["nav_radio"]
        if selected in MERGED_INTO_ACCUEIL:
            st.session_state["nav_radio"] = "🏠 Accueil"
            st.session_state["nav_page"] = "🏠 Accueil"
        else:
            st.session_state["nav_page"] = selected

    with st.sidebar:
        st.markdown(
            "<div style='text-align:center;font-size:1.6rem'>🛰️</div>"
            "<div style='text-align:center;font-weight:600'>DGRE</div>"
            "<div style='text-align:center;font-size:0.75rem;color:gray;"
            "margin-bottom:1rem'>Agent IA Satellitaire v4</div>",
            unsafe_allow_html=True,
        )
        st.radio(
            "Navigation",
            NAV_OPTIONS,
            key="nav_radio",
            index=NAV_OPTIONS.index(st.session_state["nav_page"]),
            on_change=_on_nav_change,
            label_visibility="collapsed",
        )
        st.markdown("---")
        if kpis:
            st.markdown(
                f"<div style='font-size:0.78rem;color:gray'>Total scènes</div>"
                f"<div style='font-size:1.2rem;font-weight:600'>{kpis.get('total',0)}</div>"
                f"<div style='font-size:0.78rem;color:#1D9E75'>"
                f"✅ {kpis.get('validated',0)} validées</div>"
                f"<div style='font-size:0.78rem;color:#888'>"
                f"☁️ {kpis.get('rejected',0)} rejetées</div>",
                unsafe_allow_html=True,
            )
            st.markdown("---")
        if st.button("🔓 Se déconnecter", use_container_width=True):
            st.session_state["authenticated"] = False
            st.rerun()
        st.caption(f"v4.0 · {datetime.now():%d/%m/%Y %H:%M}")
    return st.session_state["nav_page"]


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    afficher_signature()
    if not check_login():
        st.stop()

    df   = load_data()
    kpis = load_kpis()
    page = sidebar_nav(kpis)

    if page == "🏠 Accueil":
        page_accueil(df, kpis)
    elif page == "🗺️ Carte":
        page_carte(df)
    elif page == "📊 Indices":
        page_indices(df)
    elif page == "🤖 Assistant IA":
        page_chatbot()
    elif page == "🔐 Droits d'accès":
        page_droits()


if __name__ == "__main__":
    main()
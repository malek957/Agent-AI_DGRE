"""
chatbot.py — Assistant IA pour les images satellitaires DGRE.

FONCTIONNEMENT :
=================
L'utilisateur tape une question en français.
Le chatbot :
  1. Interroge le registre SQLite pour trouver les images disponibles
  2. Envoie la question + les données réelles à Groq (Llama 3.3 70B)
  3. Retourne une réponse en langage naturel avec les vraies données

EXEMPLES DE QUESTIONS :
  - "Je veux une image de Tunis du 10 août avec moins de 10% de nuages"
  - "Quelles sont les dernières images disponibles ?"
  - "Montre-moi les indices NDVI des dernières scènes"
  - "Y a-t-il des images du barrage Sidi Salem ce mois-ci ?"
  - "Quelle est la scène avec le moins de nuages ?"

LANCEMENT :
  streamlit run chatbot.py
  ou intégré dans dashboard.py comme page supplémentaire
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))
from registry import Registry
from utils.auth import check_login, logout_button
from chatbot_engine import process_chat_message

# ── Configuration ────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DB_PATH      = os.getenv("STORAGE_ROOT", "./data") + "/registry.sqlite3"
MODEL        = "llama-3.3-70b-versatile"

st.set_page_config(
    page_title="Assistant IA — DGRE Satellites",
    page_icon="🤖",
    layout="wide",
)


# ── Authentification ──────────────────────────────────────────────────────────
def check_auth():
    if not st.session_state.get("authenticated"):
        st.title("🔒 Accès restreint")
        with st.form("login"):
            user = st.text_input("Identifiant")
            pwd  = st.text_input("Mot de passe", type="password")
            ok   = st.form_submit_button("Se connecter")
        if ok:
            dashboard_user = os.getenv("DASHBOARD_USER", "admin")
            dashboard_pass = os.getenv("DASHBOARD_PASSWORD", "dgre2026")
            if user == dashboard_user and pwd == dashboard_pass:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Identifiant ou mot de passe incorrect.")
        st.stop()


# ── Chargement des données du registre ───────────────────────────────────────
def get_registry_context() -> str:
    """
    Charge les données du registre SQLite et les formate en texte
    pour que le LLM puisse les utiliser dans ses réponses.
    """
    if not Path(DB_PATH).exists():
        return "Aucune donnée disponible dans le registre."

    try:
        reg    = Registry(db_path=DB_PATH)
        scenes = reg.get_all_scenes(limit=200)
        stats  = reg.get_stats()

        if not scenes:
            return "Aucune scène enregistrée pour l'instant."

        # Résumé global
        context = f"""
STATISTIQUES GLOBALES DU CATALOGUE :
- Total scènes : {stats.get('total', 0)}
- Scènes validées et archivées : {stats.get('validated', 0)}
- Scènes rejetées (nuages) : {stats.get('rejected', 0)}
- Scènes avec indices calculés : {stats.get('with_indices', 0)}
- Taux de rejet : {stats.get('rejection_rate', 0)}%
- Nuages moyens SCL : {stats.get('avg_cloud_scl', 'N/A')}%

LISTE DES SCÈNES DISPONIBLES (les 50 plus récentes) :
"""
        # Détail des scènes
        for s in scenes[:50]:
            name         = s.get("name", "?")
            collection   = s.get("collection", "?")
            watch        = s.get("watch_name", "?")
            date         = s.get("content_date", "?")
            status       = s.get("status", "?")
            cloud_scl    = s.get("cloud_cover_scl")
            cloud_prov   = s.get("cloud_cover_provider")
            indices      = s.get("indices_computed", "")
            processed    = s.get("processed_dir", "")

            cloud_txt = (
                f"{cloud_scl:.1f}%" if cloud_scl is not None
                else f"{cloud_prov}% (fournisseur)" if cloud_prov is not None
                else "N/A"
            )

            context += f"""
---
- Scène      : {name[:60]}
- Satellite  : {collection}
- Zone       : {watch}
- Date       : {date}
- Statut     : {status}
- Nuages SCL : {cloud_txt}
- Indices    : {indices if indices else 'Non calculés'}
- Dossier    : {processed if processed else 'N/A'}
"""
        return context

    except Exception as exc:
        return f"Erreur lors du chargement du registre : {exc}"


# ── Appel à l'API Groq ────────────────────────────────────────────────────────
def ask_groq(messages: list, registry_context: str) -> str:
    """
    Envoie la conversation à Groq (Llama 3.3 70B) avec le contexte
    du registre satellitaire et retourne la réponse.
    """
    try:
        from groq import Groq
    except ImportError:
        return (
            "❌ La bibliothèque `groq` n'est pas installée.\n"
            "Exécutez : `pip install groq`"
        )

    if not GROQ_API_KEY:
        return (
            "❌ Clé API Groq manquante.\n"
            "Ajoutez `GROQ_API_KEY=gsk_...` dans votre fichier `.env`"
        )

    # Prompt système — définit le rôle et le contexte de l'assistant
    system_prompt = f"""Tu es un assistant IA spécialisé en télédétection et 
images satellitaires pour la Direction Générale des Ressources en Eau (DGRE) 
de Tunisie, sous-direction de l'eau de surface.

Tu as accès au catalogue d'images satellitaires téléchargées et traitées 
par l'agent IA de la DGRE. Tu réponds en français, de manière claire et 
concise, en utilisant les données réelles du catalogue.

Tu peux aider les utilisateurs à :
- Trouver des images disponibles selon une zone, une date, un seuil de nuages
- Expliquer les valeurs des indices spectraux (NDVI, NDWI, MNDWI, NDMI, SAVI, WDVI)
- Interpréter les résultats pour le suivi hydrologique
- Recommander les meilleures images pour une analyse donnée

GUIDE D'INTERPRÉTATION DES INDICES :
- NDVI > 0.5  : végétation dense (forêt, cultures irriguées)
- NDVI 0.2-0.5: végétation modérée (pâturages, cultures)
- NDVI < 0.2  : sol nu ou végétation très éparse
- NDVI < 0    : eau, neige, nuages
- NDWI > 0    : présence d'eau libre (barrage, oued, lac)
- MNDWI > 0   : eau en zone aride (plus fiable que NDWI en Tunisie)
- NDMI > 0.4  : végétation bien hydratée
- NDMI < 0    : sol très sec ou stress hydrique sévère

DONNÉES ACTUELLES DU CATALOGUE :
{registry_context}

Réponds toujours en te basant sur ces données réelles.
Si une image ou une zone n'est pas dans le catalogue, dis-le clairement
et suggère comment l'obtenir (relancer l'agent pour cette zone).
"""

    client = Groq(api_key=GROQ_API_KEY)

    # Construction des messages pour l'API
    api_messages = [{"role": "system", "content": system_prompt}]
    api_messages += messages

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=api_messages,
            temperature=0.3,     # Réponses précises et factuelles
            max_tokens=1024,
        )
        return response.choices[0].message.content

    except Exception as exc:
        return f"❌ Erreur API Groq : {exc}"


# ── Affichage des aperçus d'une (ou plusieurs) scène(s) ─────────────────────
def render_scenes_media(scenes_media: list):
    """
    Affiche, pour chaque scène trouvée : sa description, son composite
    couleur, sa (ses) carte(s) d'indice, et les boutons de téléchargement
    des .tif bruts.
    """
    for media in scenes_media:
        if not media.get("previews") and not media.get("raw_files"):
            continue

        st.markdown(f"**{media['description']}**")

        previews = media.get("previews", [])
        if previews:
            cols = st.columns(len(previews))
            for col, prev in zip(cols, previews):
                with col:
                    st.image(prev["png_path"], caption=prev["label"], use_container_width=True)

        raw_files = media.get("raw_files", [])
        if raw_files:
            dl_cols = st.columns(len(raw_files))
            for col, f in zip(dl_cols, raw_files):
                with col:
                    with open(f["path"], "rb") as fh:
                        st.download_button(
                            label=f"⬇️ {f['kind'].upper()} (.tif)",
                            data=fh.read(),
                            file_name=f["name"],
                            mime="image/tiff",
                            key=f"dl_{f['path']}",
                        )
        st.markdown("---")


# ── INTERFACE STREAMLIT ───────────────────────────────────────────────────────
def main():
    check_auth()

    if st.sidebar.button("🔓 Se déconnecter"):
        st.session_state["authenticated"] = False
        st.rerun()

    st.title("🤖 Assistant IA — Images Satellitaires DGRE")
    st.caption(
        "Posez vos questions en français sur les images disponibles, "
        "les indices spectraux et l'état des ressources en eau."
    )

    # Vérification de la clé API
    if not GROQ_API_KEY:
        st.error(
            "⚠️ Clé API Groq manquante. "
            "Ajoutez `GROQ_API_KEY=gsk_...` dans votre fichier `.env` "
            "puis redémarrez Streamlit."
        )
        st.stop()

    # Chargement du contexte
    with st.spinner("Chargement du catalogue..."):
        registry_context = get_registry_context()

    # Affichage du résumé du catalogue
    with st.expander("📊 Résumé du catalogue actuel", expanded=False):
        st.text(registry_context[:1000] + "...")

    st.markdown("---")

    # Initialisation de l'historique de conversation
    if "messages" not in st.session_state:
        st.session_state["messages"] = []

    # Message de bienvenue
    if not st.session_state["messages"]:
        welcome = (
            "Bonjour ! Je suis votre assistant IA pour les images satellitaires "
            "de la DGRE. Je peux vous aider à :\n\n"
            "- 🔍 **Trouver des images** selon une zone, date et seuil de nuages\n"
            "- 📊 **Interpréter les indices** NDVI, NDWI, MNDWI, NDMI, SAVI\n"
            "- 💧 **Analyser les ressources en eau** (barrages, oueds, zones humides)\n"
            "- 🌿 **Évaluer la végétation** autour des points d'eau\n\n"
            "Que puis-je faire pour vous ?"
        )
        st.session_state["messages"].append({
            "role": "assistant",
            "content": welcome,
        })

    # Affichage de l'historique
    for msg in st.session_state["messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("media"):
                render_scenes_media(msg["media"])

    # ── Suggestions de questions rapides ─────────────────────────────────────
    st.markdown("**💡 Questions rapides :**")
    col1, col2, col3 = st.columns(3)

    suggestions = {
        "🗓️ Dernières images": "Quelles sont les 5 dernières images validées disponibles ?",
        "☁️ Moins de nuages": "Quelle est la scène avec le moins de nuages dans le catalogue ?",
        "💧 Ressources en eau": "Montre-moi les images avec un NDWI élevé (présence d'eau)",
        "🌿 Végétation": "Quelles scènes ont le meilleur NDVI (végétation dense) ?",
        "📍 Barrage Sidi Salem": "Y a-t-il des images récentes du barrage Sidi Salem ?",
        "📈 Statistiques": "Donne-moi un résumé statistique du catalogue complet",
    }

    cols = [col1, col2, col3, col1, col2, col3]
    for col, (label, question) in zip(cols, suggestions.items()):
        with col:
            if st.button(label, key=f"btn_{label}", use_container_width=True):
                st.session_state["pending_question"] = question
                st.rerun()

    st.markdown("---")

    # ── Zone de saisie ────────────────────────────────────────────────────────
    user_input = st.chat_input(
        "Posez votre question sur les images satellitaires..."
    )

    # Traitement question rapide ou saisie manuelle
    question = None
    if "pending_question" in st.session_state:
        question = st.session_state.pop("pending_question")
    elif user_input:
        question = user_input

    if question:
        # Ajouter la question à l'historique
        st.session_state["messages"].append({
            "role": "user",
            "content": question,
        })

        with st.chat_message("user"):
            st.markdown(question)

        # Obtenir la réponse (recherche + éventuel téléchargement + aperçus)
        with st.chat_message("assistant"):
            with st.spinner("Recherche dans le catalogue..."):
                # Historique pour l'API (sans le message système, sans le nouveau message)
                api_history = [
                    {"role": m["role"], "content": m["content"]}
                    for m in st.session_state["messages"][:-1]
                ]
                response, downloadable_files, scenes_media = process_chat_message(
                    user_message=question,
                    chat_history=api_history,
                )

            st.markdown(response)
            if scenes_media:
                render_scenes_media(scenes_media)

        # Sauvegarder la réponse (+ aperçus, pour qu'ils réapparaissent à chaque rerun)
        st.session_state["messages"].append({
            "role": "assistant",
            "content": response,
            "media": scenes_media,
        })

    # ── Bouton réinitialiser ──────────────────────────────────────────────────
    st.markdown("---")
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🗑️ Nouvelle conversation", use_container_width=True):
            st.session_state["messages"] = []
            st.rerun()

    with col1:
        st.caption(
            f"Modèle : {MODEL} (Groq) · "
            f"Catalogue : {Path(DB_PATH).name} · "
            f"Mise à jour : {datetime.now():%H:%M:%S}"
        )


if __name__ == "__main__":
    main()
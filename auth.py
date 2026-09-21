"""
auth.py — Gestion du token d'authentification Copernicus Data Space Ecosystem.

CDSE utilise Keycloak (OAuth2). Le token d'accès expire après ~10 minutes,
donc on le régénère automatiquement quand il est proche de l'expiration.
"""

import time
import logging
import requests

logger = logging.getLogger("satellite_agent.auth")

TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE"
    "/protocol/openid-connect/token"
)


class CDSEAuth:
    """Garde le token d'accès en cache et le renouvelle automatiquement."""

    def __init__(self, username: str, password: str):
        if not username or not password:
            raise ValueError(
                "Identifiants Copernicus manquants. "
                "Vérifiez votre fichier .env (CDSE_USERNAME / CDSE_PASSWORD)."
            )
        self.username = username
        self.password = password
        self._access_token = None
        self._expires_at = 0  # timestamp unix

    def get_token(self) -> str:
        """Retourne un token valide, en le renouvelant si nécessaire."""
        # Marge de sécurité de 30s avant expiration réelle
        if self._access_token is None or time.time() > self._expires_at - 30:
            self._refresh_token()
        return self._access_token

    def _refresh_token(self):
        logger.info("Renouvellement du token d'accès Copernicus...")
        data = {
            "client_id": "cdse-public",
            "username": self.username,
            "password": self.password,
            "grant_type": "password",
        }
        try:
            response = requests.post(TOKEN_URL, data=data, timeout=30)
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(
                f"Échec de l'authentification Copernicus (HTTP {response.status_code}). "
                f"Vérifiez votre email/mot de passe. Détail: {e}"
            ) from e
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Impossible de contacter le serveur d'authentification: {e}") from e

        payload = response.json()
        self._access_token = payload["access_token"]
        expires_in = payload.get("expires_in", 600)  # secondes, ~600 par défaut
        self._expires_at = time.time() + expires_in
        logger.info(f"Token obtenu, valide {expires_in}s.")
 
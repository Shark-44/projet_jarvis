import appdaemon.plugins.hass.hassapi as hass
import yaml
import os
from datetime import datetime


class LieuResolver(hass.Hass):
    """
    Lieu Resolver — couche déduction spatiale de GEMMA

    Croise index.yaml (ce qui existe par pièce)
    avec snapshot.json (ce qui est actif)
    pour produire la liste des pièces occupées
    avec un score de certitude.

    Produit lieu_state.json lu par moteur_scoring.
    """

    def initialize(self):
        self.index_path = self.args.get(
            "index_path",
            "/config/index.yaml"
        )
        self.lieu_state_path = self.args.get(
            "lieu_state_path",
            "/config/appdaemon/apps/lieu_state.json"
        )

        # Poids des types de capteurs pour la certitude
        # Plus le capteur est fiable pour localiser → poids élevé
        self.poids_type = {
            "binary_sensor": 0.8,   # PIR — présence directe
            "sensor":        0.4,   # lux, temp — indirect
            "media_player":  0.7,   # TV active → humain au salon
            "switch":        0.3,   # switch allumé — faible signal
        }

        self.index = self._load_index()
        self.contextes_precedents = {}

        # Écoute les changements du snapshot via signal_reader
        self.signal_reader = self.get_app("signal_reader")

        # Réévaluation toutes les 15 secondes
        self.run_every(self._resoudre, "now", 15)

        self.log("LieuResolver initialisé")

    # ─────────────────────────────────────────────
    # Chargement index
    # ─────────────────────────────────────────────

    def _load_index(self):
        if not os.path.exists(self.index_path):
            self.log(f"index.yaml introuvable : {self.index_path}", level="ERROR")
            return {}
        with open(self.index_path, "r") as f:
            return yaml.safe_load(f) or {}

    # ─────────────────────────────────────────────
    # Résolution principale
    # ─────────────────────────────────────────────

    def _resoudre(self, kwargs=None):
        """
        Point d'entrée principal.
        Lit le snapshot, croise avec l'index,
        produit les contextes actifs par pièce.
        """
        snapshot = self._load_snapshot()
        if not snapshot:
            return

        # Validation Presence — gardien du pipeline
        maison_occupee = snapshot.get("maison_occupee", {}).get("value", False)
        if not maison_occupee:
            self._publier([])
            return

        # Déduction pièce par pièce
        contextes = []
        pieces = self.index.get("pieces", {})

        for piece, contenu in pieces.items():
            contexte = self._evaluer_piece(piece, contenu, snapshot)
            if contexte:
                contextes.append(contexte)

        # Tri par certitude décroissante
        contextes.sort(key=lambda c: c["certitude"], reverse=True)

        self._publier(contextes)

    # ─────────────────────────────────────────────
    # Évaluation d'une pièce
    # ─────────────────────────────────────────────

    def _evaluer_piece(self, piece, contenu, snapshot):
        """
        Pour une pièce donnée :
        - vérifie quels récepteurs sont actifs dans le snapshot
        - calcule un score de certitude
        - retourne un contexte si score > seuil
        """
        recepteurs  = contenu.get("recepteurs", [])
        appareils   = contenu.get("appareils", [])
        tous_signaux = recepteurs + appareils

        if not tous_signaux:
            return None

        signaux_actifs = []
        score_total    = 0.0
        poids_total    = 0.0

        for item in tous_signaux:
            signal_id = item.get("id")
            type_item = item.get("type", "binary_sensor")

            if not signal_id:
                continue

            entree = snapshot.get(signal_id)
            if not entree:
                continue

            valeur = entree.get("value")
            poids  = self.poids_type.get(type_item, 0.5)
            poids_total += poids

            if self._est_actif(valeur, type_item):
                signaux_actifs.append(signal_id)
                score_total += poids

        if poids_total == 0:
            return None

        certitude = round(score_total / poids_total, 2)

        # Seuil minimum pour considérer la pièce occupée
        if certitude < 0.3:
            return None

        return {
            "piece":          piece,
            "signaux_actifs": signaux_actifs,
            "certitude":      certitude,
        }

    def _est_actif(self, valeur, type_item):
        """
        Détermine si un signal indique une présence/activité.
        Chaque type de capteur a sa propre logique d'activation.
        """
        if valeur is None:
            return False

        if type_item == "binary_sensor":
            # PIR, contact, présence → True = actif
            return valeur is True

        if type_item == "media_player":
            # TV ou ampli → playing ou paused = humain présent
            return valeur in ("playing", "paused")

        if type_item == "sensor":
            # PC écran actif → on considère "on" ou valeur > 0
            if isinstance(valeur, bool):
                return valeur
            if isinstance(valeur, (int, float)):
                return valeur > 0
            return str(valeur).lower() not in ("off", "idle", "0", "unknown")

        if type_item == "switch":
            return valeur is True

        return False

    # ─────────────────────────────────────────────
    # Lecture snapshot
    # ─────────────────────────────────────────────

    def _load_snapshot(self):
        """
        Lit sensor.jarvis_signals en mémoire via signal_reader (API publique)
        ou directement via get_state si signal_reader est indisponible.
        """
        try:
            return self.signal_reader.get_snapshot()
        except Exception:
            pass

        try:
            raw = self.get_state("sensor.jarvis_signals", attribute="all") or {}
            return raw.get("attributes", {})
        except Exception as e:
            self.log(f"Erreur lecture sensor.jarvis_signals : {e}", level="ERROR")

        return {}

    # ─────────────────────────────────────────────
    # Publication
    # ─────────────────────────────────────────────

    def _publier(self, contextes):
        """
        Écrit lieu_state.json — lu par moteur_scoring.

        Exemple de sortie :
        {
          "generated_at": "2026-04-23T20:15:00",
          "contextes_actifs": [
            {
              "piece": "Bureau",
              "signaux_actifs": ["Capteur_presence_bureau", "PC_HP"],
              "certitude": 0.95
            },
            {
              "piece": "Salon",
              "signaux_actifs": ["Tele"],
              "certitude": 0.58
            }
          ]
        }
        """
        sortie = {
            "generated_at":    datetime.now().isoformat(timespec="seconds"),
            "contextes_actifs": contextes,
        }

        try:
            with open(self.lieu_state_path, "w") as f:
                json.dump(sortie, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"Erreur écriture lieu_state.json : {e}", level="ERROR")
            return

        # Log si changement
        pieces_actives = [c["piece"] for c in contextes]
        if pieces_actives != list(self.contextes_precedents.keys()):
            self.log(f"Pièces actives : {pieces_actives or 'aucune'}")
            self.contextes_precedents = {c["piece"]: c for c in contextes}

    # ─────────────────────────────────────────────
    # API publique — appelable par moteur_scoring
    # ─────────────────────────────────────────────

    def get_contextes(self):
        """Retourne la liste des contextes actifs en mémoire."""
        return list(self.contextes_precedents.values())

    def get_pieces_actives(self):
        """Retourne uniquement les noms des pièces occupées."""
        return list(self.contextes_precedents.keys())

    def est_piece_active(self, piece):
        """Retourne True si la pièce est considérée occupée."""
        return piece in self.contextes_precedents

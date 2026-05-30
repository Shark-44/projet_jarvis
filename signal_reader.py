import appdaemon.plugins.hass.hassapi as hass
import yaml
import os
from datetime import datetime


class SignalReader(hass.Hass):
    """
    Lecteur de signaux — Étape 2 du moteur GEMMA
    Lit l'index.yaml, s'abonne à toutes les entités,
    interroge Presence pour la présence humaine,
    et produit un snapshot temps réel des signaux.
    """

    def initialize(self):
        self.index_path = self.args.get(
            "index_path",
            "/homeassistant/index.yaml"
        )

        self.index    = self._load_index()
        self.snapshot = {}

        # Brique Presence — brique voisine directe
        self.presence = self.get_app("presence")

        self._subscribe_all()
        self._subscribe_presence()
        self._take_snapshot()

        self.log("SignalReader initialisé — snapshot initial produit")

    # ─────────────────────────────────────────────
    # Chargement de l'index
    # ─────────────────────────────────────────────

    def _load_index(self):
        if not os.path.exists(self.index_path):
            self.log(f"index.yaml introuvable : {self.index_path}", level="ERROR")
            return {}
        with open(self.index_path, "r") as f:
            return yaml.safe_load(f) or {}

    def _iter_entities(self):
        """
        Génère (piece, role, item) pour chaque entité de l'index.
        role = 'recepteurs' | 'actionneurs' | 'appareils'
        """
        pieces = self.index.get("pieces", {})
        for piece, contenu in pieces.items():
            for role in ("recepteurs", "actionneurs", "appareils"):
                for item in contenu.get(role, []):
                    yield piece, role, item

    # ─────────────────────────────────────────────
    # Abonnements capteurs physiques
    # ─────────────────────────────────────────────

    def _subscribe_all(self):
        count = 0
        for piece, role, item in self._iter_entities():
            entity = item.get("entity")
            if not entity:
                continue
            self.listen_state(
                self._on_state_change,
                entity,
                piece=piece,
                role=role,
                item=item,
            )
            count += 1
        self.log(f"{count} entités physiques surveillées")

    def _on_state_change(self, entity, attribute, old, new, kwargs):
        piece = kwargs["piece"]
        item  = kwargs["item"]
        self._update_snapshot(piece, item, new)
        self._write_snapshot()

    # ─────────────────────────────────────────────
    # Abonnements présence — via brique Presence
    # ─────────────────────────────────────────────

    def _subscribe_presence(self):
        """
        Écoute les trackers via person_repository,
        accessible depuis la brique Presence.
        """
        try:
            persons = self.presence.persons
            for nom in persons.get_family() + persons.get_invites():
                tracker = persons.get_tracker(nom)
                self.listen_state(
                    self._on_presence_change,
                    tracker,
                    nom=nom,
                )
            self.log("Abonnement présence OK via Presence")
        except Exception as e:
            self.log(f"Erreur abonnement présence : {e}", level="WARNING")

    def _on_presence_change(self, entity, attribute, old, new, kwargs):
        nom = kwargs["nom"]
        self._update_presence_snapshot(nom, new == "home")
        self._write_snapshot()
        self.log(f"Présence mise à jour : {nom} = {new}")

    # ─────────────────────────────────────────────
    # Snapshot
    # ─────────────────────────────────────────────

    def _take_snapshot(self):
        """Lecture initiale de toutes les entités au démarrage."""

        # 1. Capteurs physiques depuis index.yaml
        for piece, role, item in self._iter_entities():
            entity = item.get("entity")
            if not entity:
                continue
            try:
                state = self.get_state(entity)
            except Exception as e:
                state = "unavailable"
                self.log(f"Impossible de lire {entity} : {e}", level="WARNING")
            self._update_snapshot(piece, item, state)

        # 2. Présence depuis brique Presence
        self._take_presence_snapshot()

        self._write_snapshot()

    def _take_presence_snapshot(self):
        """Lit l'état de présence courant depuis la brique Presence."""
        try:
            persons  = self.presence.persons
            tous     = persons.get_family() + persons.get_invites()
            presents = persons.get_all_home()

            # Signal global maison occupée / vide
            self.snapshot["maison_occupee"] = {
                "entity":     "presence",
                "piece":      "global",
                "type":       "presence",
                "raw":        presents,
                "value":      len(presents) > 0,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }

            # Signal individuel par personne
            for nom in tous:
                est_present = nom in presents
                self.snapshot[f"presence_{nom}"] = {
                    "entity":     persons.get_tracker(nom),
                    "piece":      "global",
                    "type":       "person",
                    "raw":        "home" if est_present else "not_home",
                    "value":      est_present,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }

        except Exception as e:
            self.log(f"Erreur lecture présence : {e}", level="WARNING")

    def _update_presence_snapshot(self, nom, est_present):
        """Met à jour la présence d'une personne dans le snapshot."""
        try:
            persons  = self.presence.persons
            presents = persons.get_all_home()

            self.snapshot[f"presence_{nom}"] = {
                "entity":     persons.get_tracker(nom),
                "piece":      "global",
                "type":       "person",
                "raw":        "home" if est_present else "not_home",
                "value":      est_present,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }

            # Recalcul maison occupée
            self.snapshot["maison_occupee"]["value"]      = len(presents) > 0
            self.snapshot["maison_occupee"]["raw"]        = presents
            self.snapshot["maison_occupee"]["updated_at"] = datetime.now().isoformat(timespec="seconds")

        except Exception as e:
            self.log(f"Erreur mise à jour présence {nom} : {e}", level="WARNING")

    def _update_snapshot(self, piece, item, state):
        signal_id = item.get("id", item.get("entity"))
        entity    = item.get("entity", "")
        kind      = item.get("type", "unknown")

        value = self._normalize(kind, entity, state)

        self.snapshot[signal_id] = {
            "entity":     entity,
            "piece":      piece,
            "type":       kind,
            "raw":        state,
            "value":      value,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _normalize(self, kind, entity, raw):
        """
        Transforme l'état brut en valeur normalisée logique.
        - binary_sensor / switch / light → True / False (gère le 'on' de HA)
        - media_player                  → état string brut ("playing", "off", etc.)
        - sensor                        → float (numérique) ou valeur brute
        """
        if raw in (None, "unavailable", "unknown"):
            return None

        # Normalisation des états HA "on"/"off" en vrais booléens logiques
        if kind in ("binary_sensor", "switch", "light"):
            return raw == "on"

        if kind == "media_player":
            return raw  # "playing", "paused", "idle", "off"

        if kind == "sensor":
            try:
                return float(raw)   # Tente de convertir en numérique (ex: lux)
            except (ValueError, TypeError):
                return raw

        return raw

    def _write_snapshot(self):
        """
        Écrit le snapshot complet dans l'entité HA sensor.jarvis_signals.
        Ajoute un attribut plat 'values' pour faciliter la lecture directe par le MoteurGemma.
        """
        try:
            # Génération d'un dictionnaire plat "ID: Valeur_Normalisée" 
            # Exemple : {"Capteur_presence_cuisine": True, "PC_HP": "PowerOn"}
            flat_values = {sid: data["value"] for sid, data in self.snapshot.items()}

            # On pousse le tout dans Home Assistant
            self.set_state(
                "sensor.jarvis_signals",
                state="ok",
                attributes={
                    "metadata": self.snapshot,  # Historique complet avec "raw", "entity", "updated_at"
                    "values": flat_values       # Extrait épuré directement exploitable pour le scoring
                },
            )
        except Exception as e:
            self.log(f"Impossible d'écrire sensor.jarvis_signals : {e}", level="ERROR")

    # ─────────────────────────────────────────────
    # API publique — appelable en direct par les briques
    # ─────────────────────────────────────────────

    def get_snapshot(self):
        """Retourne le snapshot complet en mémoire."""
        return self.snapshot.copy()

    def get_signal(self, signal_id):
        """Retourne la valeur normalisée (True/False/etc.) d'un signal par son id."""
        entry = self.snapshot.get(signal_id)
        return entry["value"] if entry else None

    def get_lux(self):
        """Retourne la luminosité filtrée via l'ID exact de l'index."""
        entry = self.snapshot.get("Capteur_luminosite")
        if entry and entry["value"] is not None:
            return float(entry["value"])
        return None
import appdaemon.plugins.hass.hassapi as hass
import yaml
import json
import os
from datetime import datetime


class MoteurScoring(hass.Hass):
    """
    Moteur de scoring — Décideur central des actions JARVIS.

    Consomme :
      - snapshot["signals"]  → capteurs binaires (signal_reader.py)
      - snapshot["vectors"]  → valeurs continues FP2/LD2450 (device_map_reader.py)
      - snapshot["presence"] → présence confirmée GPS/réseau (presence.py)
      - météo HA + sun.sun   → condition météo courante
      - comportements.yaml   → matrice état × météo → actions

    Principe directeur :
      moteur_scoring ne connaît pas les capteurs physiques.
      Il consomme snapshot.json et comportements.yaml uniquement.
      La présence est un poids : maison vide → scoring suspendu.
    """

    def initialize(self):
        self.snapshot_path = self.args.get(
            "snapshot_path",
            "/config/appdaemon/apps/snapshot.json"
        )
        self.comportements_path = self.args.get(
            "comportements_path",
            "/config/appdaemon/apps/comportements.yaml"
        )
        self.gemma_state_path = self.args.get(
            "gemma_state_path",
            "/config/appdaemon/apps/gemma_state.json"
        )
        self.entity_meteo = self.args.get(
            "entity_meteo",
            "weather.meteofrance_maison"
        )
        self.entity_sun = self.args.get(
            "entity_sun",
            "sun.sun"
        )
        self.temp_seuil_chaud = self.args.get(
            "temp_seuil_chaud", 22
        )
        self.index_path = self.args.get(
            "index_path", "/config/index.yaml"
        )

        self.comportements    = self._load_comportements()
        self.etat_precedent   = None
        self.meteo_precedente = None

        # Déclencheur : changement d'état GEMMA
        self.listen_state(
            self._on_gemma_change,
            self.args.get("entity_etat", "input_text.gemma_etat_courant")
        )

        # Déclencheur : changement météo ou soleil
        self.listen_state(self._on_meteo_change, self.entity_meteo)
        self.listen_state(self._on_meteo_change, self.entity_sun)

        self.log("Moteur de scoring initialisé")

    # ─────────────────────────────────────────────
    # Chargement comportements
    # ─────────────────────────────────────────────

    def _load_comportements(self):
        if not os.path.exists(self.comportements_path):
            self.log("comportements.yaml introuvable", level="ERROR")
            return {}
        with open(self.comportements_path, "r") as f:
            return yaml.safe_load(f) or {}

    # ─────────────────────────────────────────────
    # Déclencheurs
    # ─────────────────────────────────────────────

    def _on_gemma_change(self, entity, attribute, old, new, kwargs):
        if new != old:
            self.log(f"GEMMA changement détecté : {old} → {new}")
            self._appliquer(etat=new)

    def _on_meteo_change(self, entity, attribute, old, new, kwargs):
        meteo = self._lire_condition_meteo()
        if meteo != self.meteo_precedente:
            self.log(f"Météo changement : {self.meteo_precedente} → {meteo}")
            self.meteo_precedente = meteo
            etat = self._lire_etat_gemma()
            if etat:
                self._appliquer(etat=etat, meteo=meteo)

    # ─────────────────────────────────────────────
    # Lecture présence (snapshot["presence"])
    # ─────────────────────────────────────────────

    def _lire_presence(self):
        """
        Lit le bloc presence dans snapshot.json.
        Défaut sécurisé : maison_occupee=True si lecture impossible.
        (On ne risque pas d'éteindre si doute.)
        """
        try:
            if os.path.exists(self.snapshot_path):
                with open(self.snapshot_path, "r") as f:
                    data = json.load(f)
                    return data.get("presence", {"maison_occupee": True})
        except Exception as e:
            self.log(f"Erreur lecture presence dans snapshot : {e}", level="WARNING")
        return {"maison_occupee": True}

    # ─────────────────────────────────────────────
    # Lecture état GEMMA
    # ─────────────────────────────────────────────

    def _lire_etat_gemma(self):
        try:
            if os.path.exists(self.gemma_state_path):
                with open(self.gemma_state_path, "r") as f:
                    data = json.load(f)
                    return data.get("etat_courant")
        except Exception as e:
            self.log(f"Erreur lecture gemma_state.json : {e}", level="ERROR")
        return None

    # ─────────────────────────────────────────────
    # Lecture météo
    # ─────────────────────────────────────────────

    def _lire_condition_meteo(self):
        """
        Mappe les états Météo France + sun.sun vers les clés de comportements.yaml.
        Priorité : sun.sun (nuit) > pluie > nuageux > dégagé (chaud/froid).
        """
        try:
            # 1. Nuit ? (sun.sun below_horizon)
            sun_state = self.get_state(self.entity_sun)
            if sun_state == "below_horizon":
                return "nuit"

            # 2. Condition ciel Météo France
            ciel = self.get_state(self.entity_meteo)

            # 3. Température actuelle
            temp = self.get_state(self.entity_meteo, attribute="temperature")
            try:
                temp = float(temp)
            except (TypeError, ValueError):
                temp = 15.0

            conditions_pluie    = {"rainy", "pouring", "lightning", "lightning-rainy", "hail"}
            conditions_nuageuses = {"cloudy", "partlycloudy", "fog", "windy"}

            if ciel in conditions_pluie:
                return "pluvieux"

            if ciel in conditions_nuageuses:
                return "nuageux"

            # Dégagé : chaud ou froid selon température
            return "degage_chaud" if temp > self.temp_seuil_chaud else "degage_froid"

        except Exception as e:
            self.log(f"Erreur lecture météo : {e}", level="WARNING")
            return "nuageux"  # repli conservateur

    # ─────────────────────────────────────────────
    # Application des comportements
    # ─────────────────────────────────────────────

    def _appliquer(self, etat=None, meteo=None):
        # ── Guard 1 : présence ──────────────────
        presence = self._lire_presence()
        if not presence.get("maison_occupee", True):
            self.log(
                f"Maison vide (présence confirmée) — scoring suspendu. "
                f"Dernière maj : {presence.get('derniere_maj', 'inconnue')}"
            )
            return

        # ── Guard 2 : état valide ───────────────
        if not etat:
            etat = self._lire_etat_gemma()
        if not meteo:
            meteo = self._lire_condition_meteo()

        if not etat or etat == "INDETERMINE":
            self.log(f"État {etat!r} — aucune action")
            return

        # ── Résolution comportement ─────────────
        etats_config = self.comportements.get("etats", {})
        config_etat  = etats_config.get(etat)

        if not config_etat:
            self.log(f"Aucun comportement défini pour : {etat}", level="WARNING")
            return

        actions = config_etat.get(meteo)
        if not actions:
            self.log(f"Aucune action pour {etat} × {meteo}", level="WARNING")
            return

        personnes = presence.get("personnes_home", [])
        self.log(
            f"Application : {etat} × {meteo} → {len(actions)} actionneurs "
            f"| présents : {personnes}"
        )

        for actionneur_id, valeur in actions.items():
            self._actionner(actionneur_id, valeur)

    # ─────────────────────────────────────────────
    # Actionnement entité
    # ─────────────────────────────────────────────

    def _actionner(self, actionneur_id, valeur):
        """
        Actionne une entité selon sa valeur cible.
          bool        → switch on/off
          int/float   → lumière dimmable (0 = off, 1-255 = brightness)
        """
        entity = self._id_vers_entity(actionneur_id)
        if not entity:
            self.log(f"Entité introuvable pour : {actionneur_id}", level="WARNING")
            return

        try:
            if isinstance(valeur, bool):
                self.turn_on(entity) if valeur else self.turn_off(entity)

            elif isinstance(valeur, (int, float)):
                if valeur == 0:
                    self.turn_off(entity)
                else:
                    self.turn_on(entity, brightness=int(valeur))

        except Exception as e:
            self.log(f"Erreur action sur {entity} : {e}", level="ERROR")

    # ─────────────────────────────────────────────
    # Résolution id → entity_id HA
    # ─────────────────────────────────────────────

    def _id_vers_entity(self, actionneur_id):
        """
        Résout un id d'actionneur vers son entity_id HA.
        Priorité 1 : snapshot["signals"]
        Priorité 2 : index.yaml
        """
        # Priorité 1 — snapshot signals
        try:
            if os.path.exists(self.snapshot_path):
                with open(self.snapshot_path, "r") as f:
                    data = json.load(f)
                signals = data.get("signals", {})
                if actionneur_id in signals:
                    return signals[actionneur_id].get("entity")
        except Exception:
            pass

        # Priorité 2 — index.yaml
        try:
            if os.path.exists(self.index_path):
                with open(self.index_path, "r") as f:
                    index = yaml.safe_load(f)
                for piece, contenu in index.get("pieces", {}).items():
                    for item in contenu.get("actionneurs", []):
                        if item.get("id") == actionneur_id:
                            return item.get("entity")
        except Exception:
            pass

        return None

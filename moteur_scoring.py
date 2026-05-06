import appdaemon.plugins.hass.hassapi as hass
import yaml
import json
import os
from datetime import datetime


class MoteurScoring(hass.Hass):
    """
    Moteur de scoring — Étape 4
    Lit gemma_state.json (état GEMMA) + météo HA + sun.sun
    Consulte comportements.yaml
    Actionne les lumières et switches en conséquence.
    """

    def initialize(self):
        self.gemma_state_path = self.args.get(
            "gemma_state_path",
            "/config/appdaemon/apps/gemma_state.json"
        )
        self.comportements_path = self.args.get(
            "comportements_path",
            "/config/appdaemon/apps/comportements.yaml"
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

        self.comportements = self._load_comportements()
        self.etat_precedent = None
        self.meteo_precedente = None

        # Réagit à chaque changement d'état GEMMA
        self.listen_state(self._on_gemma_change, self.args.get(
            "entity_etat", "input_text.gemma_etat_courant"
        ))

        # Réagit aussi aux changements météo
        self.listen_state(self._on_meteo_change, self.entity_meteo)
        self.listen_state(self._on_meteo_change, self.entity_sun)

        self.log("Moteur de scoring initialisé")

    # ─────────────────────────────────────────────
    # Chargement comportements
    # ─────────────────────────────────────────────

    def _load_comportements(self):
        if not os.path.exists(self.comportements_path):
            self.log(f"comportements.yaml introuvable", level="ERROR")
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
    # Lecture état et météo
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

    def _lire_condition_meteo(self):
        """
        Mappe les états Météo France + sun.sun vers les clés de comportements.yaml
        Priorité : sun.sun (nuit) > température > condition ciel
        """
        try:
            # 1. Nuit ? (sun.sun below_horizon)
            sun_state = self.get_state(self.entity_sun)
            if sun_state == "below_horizon":
                return "nuit"

            # 2. Condition ciel Météo France
            ciel = self.get_state(self.entity_meteo)

            # 3. Température actuelle
            temp = self.get_state(
                self.entity_meteo,
                attribute="temperature"
            )
            try:
                temp = float(temp)
            except (TypeError, ValueError):
                temp = 15.0

            # Mapping conditions Météo France → clés comportements
            conditions_chaudes = {"sunny", "clear-night", "partlycloudy"}
            conditions_nuageuses = {"cloudy", "partlycloudy", "fog", "windy"}
            conditions_pluie = {"rainy", "pouring", "lightning", "lightning-rainy", "hail"}

            if ciel in conditions_pluie:
                return "pluvieux"

            if ciel in conditions_nuageuses and ciel not in conditions_chaudes:
                return "nuageux"

            # Dégagé : chaud ou froid selon température
            if temp > self.temp_seuil_chaud:
                return "degage_chaud"
            else:
                return "degage_froid"

        except Exception as e:
            self.log(f"Erreur lecture météo : {e}", level="WARNING")
            return "nuageux"  # valeur de repli

    # ─────────────────────────────────────────────
    # Application des comportements
    # ─────────────────────────────────────────────

    def _appliquer(self, etat=None, meteo=None):
        if not etat:
            etat = self._lire_etat_gemma()
        if not meteo:
            meteo = self._lire_condition_meteo()

        if etat == "INDETERMINE":
            self.log("État INDÉTERMINÉ — aucune action")
            return

        etats_config = self.comportements.get("etats", {})
        config_etat  = etats_config.get(etat)

        if not config_etat:
            self.log(f"Aucun comportement défini pour : {etat}", level="WARNING")
            return

        actions = config_etat.get(meteo)
        if not actions:
            self.log(f"Aucune action pour {etat} × {meteo}", level="WARNING")
            return

        self.log(f"Application : {etat} × {meteo} → {len(actions)} actionneurs")

        for actionneur_id, valeur in actions.items():
            self._actionner(actionneur_id, valeur)

    def _actionner(self, actionneur_id, valeur):
        """
        Actionne une entité selon sa valeur cible.
        - int/float  → lumière dimmable (0 = off, 1-255 = luminosité)
        - True/False → switch on/off
        """
        # Retrouver l'entity_id depuis l'id de l'index
        entity = self._id_vers_entity(actionneur_id)
        if not entity:
            self.log(f"Entité introuvable pour : {actionneur_id}", level="WARNING")
            return

        try:
            if isinstance(valeur, bool):
                # Switch
                if valeur:
                    self.turn_on(entity)
                else:
                    self.turn_off(entity)

            elif isinstance(valeur, int) or isinstance(valeur, float):
                if valeur == 0:
                    self.turn_off(entity)
                else:
                    # Lumière dimmable — brightness 0-255
                    self.turn_on(entity, brightness=int(valeur))

        except Exception as e:
            self.log(f"Erreur action sur {entity} : {e}", level="ERROR")

    # ─────────────────────────────────────────────
    # Résolution id → entity_id HA
    # ─────────────────────────────────────────────

    def _id_vers_entity(self, actionneur_id):
        """
        Cherche l'entity_id correspondant à un id d'actionneur
        en lisant directement le snapshot (qui contient pièces + entités).
        Fallback : on essaie de deviner depuis le nom.
        """
        snapshot_path = self.args.get(
            "snapshot_path",
            "/config/appdaemon/apps/snapshot.json"
        )
        try:
            if os.path.exists(snapshot_path):
                with open(snapshot_path, "r") as f:
                    data = json.load(f)
                signals = data.get("signals", {})
                if actionneur_id in signals:
                    return signals[actionneur_id].get("entity")
        except Exception:
            pass

        # Dernier recours : cherche dans index.yaml
        index_path = self.args.get(
            "index_path", "/config/index.yaml"
        )
        try:
            if os.path.exists(index_path):
                with open(index_path, "r") as f:
                    index = yaml.safe_load(f)
                for piece, contenu in index.get("pieces", {}).items():
                    for item in contenu.get("actionneurs", []):
                        if item.get("id") == actionneur_id:
                            return item.get("entity")
        except Exception:
            pass

        return None

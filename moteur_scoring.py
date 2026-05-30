import appdaemon.plugins.hass.hassapi as hass
import yaml
import json
import os
from datetime import datetime


class MoteurScoring(hass.Hass):

    def initialize(self):
        # ── Configuration des chemins et entités existantes ──
        self.comportements_path = self.args.get("comportements_path", "/homeassistant/comportements.yaml")
        self.gemma_state_path = self.args.get("gemma_state_path", "/homeassistant/gemma_state.json")
        self.entity_meteo = self.args.get("entity_meteo", "weather.frossay")
        self.entity_sun = self.args.get("entity_sun", "sun.sun")
        self.index_path = self.args.get("index_path", "/homeassistant/index.yaml")
        self.entity_gemma_etats = self.args.get("entity_gemma_etats", "sensor.jarvis_gemma_etats")
        
        # ── AJOUT : Suivi des lieux et des minuteurs ──
        self.entity_jarvis_lieu = "sensor.jarvis_lieu"
        self.minuteurs_extinction = {} # Stocke { "Chambre": handle_du_run_in }
        self.TEMPO_EXTINCTION = 5 * 60  # 5 minutes en secondes
        # ──────────────────────────────────────────────

        self.comportements    = self._load_comportements()
        self.meteo_precedente = None

        # Déclencheurs existants
        self.listen_state(self._on_gemma_change, self.entity_gemma_etats, attribute="all")
        self.listen_state(self._on_meteo_change, self.entity_meteo)
        self.listen_state(self._on_meteo_change, self.entity_sun)

        # ── AJOUT : Déclencheur de surveillance spatiale ──
        self.listen_state(self._on_lieu_change, self.entity_jarvis_lieu)
        
        # ── CADENCE 60S : Alignement avec Moteur Gemma ──
        self.run_every(self._cb_cadence, "now", 60)
        # ──────────────────────────────────────────────────

        self.log("=" * 40)
        self.log("  MoteurScoring v3.1 — Donneur d'ordre & Extinction (Fix 60s)")
        self.log("=" * 40)

    def _load_comportements(self):
        if not os.path.exists(self.comportements_path):
            self.log("comportements.yaml introuvable", level="ERROR")
            return {}
        with open(self.comportements_path, "r") as f:
            return yaml.safe_load(f) or {}

    def _cb_cadence(self, kwargs):
        """Callback pour forcer l'application toutes les 60 secondes."""
        self._appliquer()

    # ─────────────────────────────────────────────
    # NOUVEAU METIER : Gestion Spatiale & Minuteurs
    # ─────────────────────────────────────────────

    def _on_lieu_change(self, entity, attribute, old, new, kwargs):
        """Écoute sensor.jarvis_lieu pour orchestrer les minuteurs d'extinction."""
        try:
            anciennes_pieces = set(json.loads(old)) if old else set()
            nouvelles_pieces = set(json.loads(new)) if new else set()
        except Exception as e:
            return

        # 1. Détection des pièces qui viennent de disparaître
        pieces_disparues = anciennes_pieces - nouvelles_pieces
        for piece in pieces_disparues:
            if piece not in self.minuteurs_extinction:
                self.log(f"[SPATIAL] Disparition de : {piece}. Planification de l'extinction dans 5 min.")
                self.minuteurs_extinction[piece] = self.run_in(self._cb_extinction, self.TEMPO_EXTINCTION, piece=piece)

        # 2. Anti-extinction : Si une pièce réapparaît, on annule immédiatement son minuteur
        for piece in nouvelles_pieces:
            if piece in self.minuteurs_extinction:
                self.log(f"[SPATIAL] Humain de retour dans : {piece}. Annulation du minuteur d'extinction.")
                self.cancel_timer(self.minuteurs_extinction[piece])
                del self.minuteurs_extinction[piece]

    def _cb_extinction(self, kwargs):
        """Callback appelé par le minuteur AppDaemon une fois les 5 minutes écoulées."""
        piece = kwargs.get("piece")
        self.log(f"[DONNEUR D'ORDRE] Minuteur écoulé pour : {piece}. Exécution de l'ordre d'extinction.")
        
        if piece in self.minuteurs_extinction:
            del self.minuteurs_extinction[piece]

        self._appliquer_extinction_directe(piece)

    def _appliquer_extinction_directe(self, piece):
        nom_etat = f"EXTINCTION_{piece}"
        etats_config = self.comportements.get("etats", {})
        config_extinction = etats_config.get(nom_etat)

        if not config_extinction:
            self.log(f"[REGLAGE] Aucun état '{nom_etat}' défini dans comportements.yaml", level="WARNING")
            return

        self.log(f"║ ── EXTINCTION ABSOLUE : {nom_etat} ──")
        
        for actionneur_id, valeur in config_extinction.items():
            entity = self._id_vers_entity(actionneur_id)
            if not entity:
                continue
            resultat = self._actionner_log(entity, valeur)
            self.log(f"║     {'✅' if resultat else '❌'} {actionneur_id!r} → {entity}  valeur={valeur}")

    # ─────────────────────────────────────────────
    # Déclencheurs et Logique d'Allumage Existante
    # ─────────────────────────────────────────────

    def _on_gemma_change(self, entity, attribute, old, new, kwargs):
        self._appliquer()

    def _on_meteo_change(self, entity, attribute, old, new, kwargs):
        meteo = self._lire_condition_meteo()
        if meteo != self.meteo_precedente:
            self.meteo_precedente = meteo
            self._appliquer(meteo=meteo)

    def _lire_presence(self):
        try:
            raw = self.get_state("sensor.jarvis_presence", attribute="all") or {}
            attrs = raw.get("attributes", {})
            if attrs: return attrs
        except Exception as e:
            self.log(f"[PRÉSENCE] Erreur lecture : {e}", level="WARNING")
        return {"maison_occupee": True, "personnes_home": []}

    def _lire_etats_gemma(self):
        try:
            if os.path.exists(self.gemma_state_path):
                with open(self.gemma_state_path, "r") as f:
                    data = json.load(f)
                    return data.get("personnes", {})
        except Exception as e:
            self.log(f"[GEMMA] Erreur lecture gemma_state.json : {e}", level="ERROR")
        return {}

    def _lire_condition_meteo(self):
        try:
            sun_state = self.get_state(self.entity_sun)
            if sun_state == "below_horizon": return "nuit"

            vectors_attrs = self.get_state("sensor.jarvis_vectors", attribute="all") or {}
            meteo_locale = vectors_attrs.get("attributes", {}).get("Meteo_locale", {})
            sorties_derivees = meteo_locale.get("sorties_derivees", {})
            
            if sorties_derivees.get("pleut", False): return "pluie"
            if sorties_derivees.get("besoin_lumiere", False): return "sombre"
            return "soleil"
        except Exception as e:
            self.log(f"[MÉTÉO LOCAL] Erreur calcul environnement : {e}", level="WARNING")
            return "soleil"

    def _appliquer(self, meteo=None):
        now = datetime.now().strftime("%H:%M:%S")
        self.log(f"**** _appliquer() [{now}] ***********")

        presence = self._lire_presence()
        if not presence.get("maison_occupee", True):
            return

        if not meteo:
            meteo = self._lire_condition_meteo()

        etats_personnes = self._lire_etats_gemma()
        if not etats_personnes:
            return

        etats_config = self.comportements.get("etats", {})
        actions_finales = {}
        scores_finaux = {}

        for personne, data in etats_personnes.items():
            etat = data.get("etat")
            score = data.get("score", 0.0)

            if not etat or etat == "INDETERMINE": continue

            config_etat = etats_config.get(etat)
            if not config_etat: continue

            actions = config_etat.get(meteo)
            if not actions: continue

            for actionneur_id, valeur in actions.items():
                score_existant = scores_finaux.get(actionneur_id, -1)
                if score > score_existant:
                    actions_finales[actionneur_id] = valeur
                    scores_finaux[actionneur_id] = score

        if not actions_finales: return

        for actionneur_id, valeur in actions_finales.items():
            entity = self._id_vers_entity(actionneur_id)
            if entity:
                self._actionner_log(entity, valeur)

    def _actionner_log(self, entity, valeur):
        try:
            if isinstance(valeur, bool):
                self.turn_on(entity) if valeur else self.turn_off(entity)
            elif isinstance(valeur, (int, float)):
                if valeur == 0:
                    self.turn_off(entity)
                else:
                    self.turn_on(entity, brightness=int(valeur))
            return True
        except Exception as e:
            self.log(f"[ACTION] Erreur sur {entity} : {e}", level="ERROR")
            return False

    def _id_vers_entity(self, actionneur_id):
        """Traduit un ID du YAML en entité Home Assistant réelle."""
        try:
            raw = self.get_state("sensor.jarvis_signals", attribute="all") or {}
            attrs = raw.get("attributes", {})
            # FIX : Les correspondances sont rangées sous la clé 'metadata'
            signals = attrs.get("metadata", {})
            
            if actionneur_id in signals:
                return signals[actionneur_id].get("entity")
        except Exception: pass

        try:
            if os.path.exists(self.index_path):
                with open(self.index_path, "r") as f:
                    index = yaml.safe_load(f)
                for piece, contenu in index.get("pieces", {}).items():
                    for item in contenu.get("actionneurs", []):
                        if item.get("id") == actionneur_id:
                            return item.get("entity")
        except Exception: pass
        return None
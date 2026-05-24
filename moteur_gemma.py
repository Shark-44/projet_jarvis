import appdaemon.plugins.hass.hassapi as hass
import yaml
import json
import os
from datetime import datetime


class MoteurGemma(hass.Hass):
    """
    Moteur GEMMA — v2
    =================
    Lit sensor.jarvis_signals (signal_reader.py)
    et sensor.jarvis_vectors (device_map_reader.py),
    calcule le score de chaque état défini dans poids.yaml,
    et publie l'état courant.

    Structure sensor.jarvis_vectors attendue (v2) :
      {
        "Presence_bureau": {
          "piece":            "bureau",
          "timestamp":        "...",
          "sorties":          { "presence_z1": true, ... },
          "sorties_derivees": { "en_lecture": false, ... },
          "vecteurs":         { "cible_1_vitesse": 12.3, ... }
        },
        ...
      }

    Injection dans signals :
      sorties          → {device_id}_{cle}         ex: Presence_bureau_presence_z1
      sorties_derivees → {device_id}_{cle}         ex: Tele_en_lecture
      vecteurs         → non injectés (futur moteur)
    """

    def initialize(self):
        self.poids_path = self.args.get(
            "poids_path",
            "/homeassistant/poids.yaml"
        )
        self.gemma_state_path = self.args.get(
            "gemma_state_path",
            "/homeassistant/gemma_state.json"
        )
        self.entity_etat = self.args.get(
            "entity_etat",
            "input_text.gemma_etat_courant"
        )

        self.poids = self._load_poids()
        self.etat_courant = None

        # Réévaluation toutes les 30 secondes
        self.run_every(self._evaluer, "now", 30)

        self.log("Moteur GEMMA v2 initialise")

    # ─────────────────────────────────────────────
    # Chargement de la config
    # ─────────────────────────────────────────────

    def _load_poids(self):
        if not os.path.exists(self.poids_path):
            self.log(f"poids.yaml introuvable : {self.poids_path}", level="ERROR")
            return {}
        with open(self.poids_path, "r") as f:
            return yaml.safe_load(f) or {}

    # ─────────────────────────────────────────────
    # Lecture snapshot
    # ─────────────────────────────────────────────

    def _load_snapshot(self):
        """
        Reconstruit le dict signals depuis les deux entités HA.

        sensor.jarvis_signals → signals directs (signal_reader.py)
        sensor.jarvis_vectors → sorties + sorties_derivees (device_map_reader.py)

        Les vecteurs bruts ne sont PAS injectés — réservés au futur moteur.

        Format de chaque entrée signals :
          { "value": <valeur>, "piece": <piece>, "type": <type> }
        """
        signals = {}

        # ── 1. Signaux directs (signal_reader.py) ──────────────────
        try:
            raw = self.get_state("sensor.jarvis_signals", attribute="all") or {}
            signals.update(raw.get("attributes", {}))
        except Exception as e:
            self.log(f"Erreur lecture sensor.jarvis_signals : {e}", level="WARNING")

        # ── 2. Vectors (device_map_reader.py) ──────────────────────
        try:
            raw = self.get_state("sensor.jarvis_vectors", attribute="all") or {}
            vectors = raw.get("attributes", {})

            for device_id, vecteur in vectors.items():
                if not isinstance(vecteur, dict):
                    continue

                piece = vecteur.get("piece")

                # sorties — binaires directs on/off
                for cle, val in vecteur.get("sorties", {}).items():
                    signals[f"{device_id}_{cle}"] = {
                        "value": val,
                        "piece": piece,
                        "type":  "sortie",
                    }

                # sorties_derivees — binaires calculés par device_map_reader
                for cle, val in vecteur.get("sorties_derivees", {}).items():
                    signals[f"{device_id}_{cle}"] = {
                        "value": val,
                        "piece": piece,
                        "type":  "sortie_derivee",
                    }

                # vecteurs — NON injectés, futur moteur uniquement

        except Exception as e:
            self.log(f"Erreur lecture sensor.jarvis_vectors : {e}", level="WARNING")

        return signals

    # ─────────────────────────────────────────────
    # Évaluation principale
    # ─────────────────────────────────────────────

    def _evaluer(self, kwargs=None):
        signals = self._load_snapshot()
        self.log(f"Signaux disponibles : {sorted(signals.keys())}")
        if not signals:
            return

        heure = datetime.now().hour
        modif = self._modificateur_contexte(heure)
        seuil = self.poids.get("seuil", 0.75)
        etats = self.poids.get("etats", {})

        scores = {}
        for nom_etat, config in etats.items():
            score = self._calculer_score(signals, config, modif)
            self.log(f"  {nom_etat} → {round(score, 3)}")
            scores[nom_etat] = round(score, 3)

        scores_tries = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        meilleur_etat, meilleur_score = scores_tries[0]
        nouvel_etat = meilleur_etat if meilleur_score >= seuil else "INDETERMINE"

        self._publier(nouvel_etat, meilleur_score, scores, heure, modif)

    # ─────────────────────────────────────────────
    # Calcul du score d'un état
    # ─────────────────────────────────────────────

    def _calculer_score(self, signals, config_etat, modif_contexte):
        """
        Score = Σ(contribution_signal) / Σ(poids_max_possibles)
        Normalisé entre 0 et 1.

        Un signal absent ne contribue pas mais n'est pas pénalisé.
        Un signal négatif (poids < 0) réduit le score si actif.
        """
        signaux_config = config_etat.get("signaux", {})
        if not signaux_config:
            return 0.0

        score_total = 0.0
        poids_total = 0.0

        for signal_id, params in signaux_config.items():
            poids = params.get("poids", 0.5)
            poids_total += abs(poids)

            valeur = self._lire_valeur(signals, signal_id)
            if valeur is None:
                continue

            contribution = self._evaluer_signal(valeur, params) * poids
            score_total += contribution

        if poids_total == 0:
            return 0.0

        score_brut = score_total / poids_total
        return min(max(score_brut * modif_contexte, 0.0), 1.0)

    def _lire_valeur(self, signals, signal_id):
        entry = signals.get(signal_id)
        if not entry:
            return None
        return entry.get("value")

    def _evaluer_signal(self, valeur, params):
        """
        Retourne 1.0 si le signal correspond au critère, 0.0 sinon.
          - valeur_active : correspondance exacte (bool ou string)
          - seuil_max     : valeur <= seuil_max
          - seuil_min     : valeur >= seuil_min
        """
        if "valeur_active" in params:
            attendu = params["valeur_active"]
            if isinstance(attendu, bool):
                return 1.0 if bool(valeur) == attendu else 0.0
            return 1.0 if str(valeur) == str(attendu) else 0.0

        if "seuil_max" in params:
            try:
                return 1.0 if float(valeur) <= float(params["seuil_max"]) else 0.0
            except (TypeError, ValueError):
                return 0.0

        if "seuil_min" in params:
            try:
                return 1.0 if float(valeur) >= float(params["seuil_min"]) else 0.0
            except (TypeError, ValueError):
                return 0.0

        return 0.0

    # ─────────────────────────────────────────────
    # Modificateur contextuel (heure)
    # ─────────────────────────────────────────────

    def _modificateur_contexte(self, heure):
        contextes = self.poids.get("contexte", {})
        for nom, cfg in contextes.items():
            debut = cfg.get("debut", 0)
            fin   = cfg.get("fin", 24)
            modif = cfg.get("modificateur", 1.0)
            if debut > fin:
                if heure >= debut or heure < fin:
                    return modif
            else:
                if debut <= heure < fin:
                    return modif
        return 1.0

    # ─────────────────────────────────────────────
    # Publication du résultat
    # ─────────────────────────────────────────────

    def _publier(self, etat, score, tous_scores, heure, modif):
        maintenant = datetime.now().isoformat(timespec="seconds")

        sortie = {
            "generated_at": maintenant,
            "etat_courant": etat,
            "score":        score,
            "heure":        heure,
            "modificateur": modif,
            "tous_scores":  tous_scores,
        }

        try:
            with open(self.gemma_state_path, "w") as f:
                json.dump(sortie, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"Erreur écriture gemma_state.json : {e}", level="ERROR")

        try:
            self.set_state(
                self.entity_etat,
                state=etat,
                attributes={
                    "score":         score,
                    "tous_scores":   tous_scores,
                    "updated_at":    maintenant,
                    "friendly_name": "GEMMA — État courant",
                }
            )
        except Exception as e:
            self.log(f"Erreur mise à jour entity HA : {e}", level="WARNING")

        if etat != self.etat_courant:
            self.log(f"État GEMMA : {self.etat_courant} → {etat} (score={score})")
            self.etat_courant = etat

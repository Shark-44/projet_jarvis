import appdaemon.plugins.hass.hassapi as hass
import yaml
import json
import os
from datetime import datetime


class MoteurGemma(hass.Hass):
    """
    Moteur GEMMA — Étape 3
    Lit le snapshot.json produit par SignalReader et DeviceMapReader,
    calcule le score de chaque état, et publie l'état courant.
    """

    def initialize(self):
        self.snapshot_path = self.args.get(
            "snapshot_path",
            "/config/appdaemon/apps/snapshot.json"
        )
        self.poids_path = self.args.get(
            "poids_path",
            "/config/appdaemon/apps/poids.yaml"
        )
        self.gemma_state_path = self.args.get(
            "gemma_state_path",
            "/config/appdaemon/apps/gemma_state.json"
        )
        self.entity_etat = self.args.get(
            "entity_etat",
            "input_text.gemma_etat_courant"
        )

        self.poids = self._load_poids()
        self.etat_courant = None

        # Réévaluation toutes les 30 secondes
        self.run_every(self._evaluer, "now", 30)

        self.log("Moteur GEMMA initialisé")

    # ─────────────────────────────────────────────
    # Chargement de la config
    # ─────────────────────────────────────────────

    def _load_poids(self):
        if not os.path.exists(self.poids_path):
            self.log(f"poids.yaml introuvable : {self.poids_path}", level="ERROR")
            return {}
        with open(self.poids_path, "r") as f:
            return yaml.safe_load(f) or {}

    def _load_snapshot(self):
        if not os.path.exists(self.snapshot_path):
            self.log("snapshot.json introuvable — SignalReader est-il lancé ?", level="WARNING")
            return {}
        with open(self.snapshot_path, "r") as f:
            data = json.load(f)

        signals = data.get("signals", {})
        vectors = data.get("vectors", {})

        for device_id, vecteur in vectors.items():
            if vecteur.get("etat_derive"):
                signals[f"{device_id}_etat"] = {
                    "value": vecteur.get("etat_derive"),
                    "piece": vecteur.get("piece"),
                    "type":  "etat_derive",
                }
            for key, val in vecteur.get("valeurs", {}).items():
                signals[f"{device_id}_{key}"] = {
                    "value": val,
                    "piece": vecteur.get("piece"),
                    "type":  "vector",
                }

        return signals

    # ─────────────────────────────────────────────
    # Évaluation principale
    # ─────────────────────────────────────────────

    def _evaluer(self, kwargs=None):
        signals = self._load_snapshot()
        if not signals:
            return

        heure = datetime.now().hour
        modif = self._modificateur_contexte(heure)
        seuil = self.poids.get("seuil", 0.75)
        etats = self.poids.get("etats", {})

        scores = {}
        for nom_etat, config in etats.items():
            score = self._calculer_score(signals, config, modif)
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
        """
        signaux_config = config_etat.get("signaux", {})
        if not signaux_config:
            return 0.0

        score_total = 0.0
        poids_total = 0.0

        for signal_id, params in signaux_config.items():
            poids = params.get("poids", 0.5)
            poids_total += poids

            valeur = self._lire_valeur(signals, signal_id)
            if valeur is None:
                continue

            contribution = self._evaluer_signal(valeur, params) * poids
            score_total += contribution

        if poids_total == 0:
            return 0.0

        score_brut = score_total / poids_total
        return min(score_brut * modif_contexte, 1.0)

    def _lire_valeur(self, signals, signal_id):
        entry = signals.get(signal_id)
        if not entry:
            return None
        return entry.get("value")

    def _evaluer_signal(self, valeur, params):
        """
        Retourne 1.0 si le signal correspond au critère, 0.0 sinon.
          - valeur_active : correspondance exacte (bool ou string)
          - seuil_max     : la valeur doit être <= seuil_max
          - seuil_min     : la valeur doit être >= seuil_min
        """
        if "valeur_active" in params:
            attendu = params["valeur_active"]
            if isinstance(attendu, bool):
                return 1.0 if bool(valeur) == attendu else 0.0
            return 1.0 if str(valeur) == str(attendu) else 0.0

        if "seuil_max" in params:
            try:
                return 1.0 if float(valeur) <= params["seuil_max"] else 0.0
            except (TypeError, ValueError):
                return 0.0

        if "seuil_min" in params:
            try:
                return 1.0 if float(valeur) >= params["seuil_min"] else 0.0
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
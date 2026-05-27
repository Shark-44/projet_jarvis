import appdaemon.plugins.hass.hassapi as hass
import yaml
import json
import os
from datetime import datetime


class MoteurGemma(hass.Hass):
    """
    Moteur GEMMA — v3.0
    ====================
    Évalue un état par personne présente dans la maison.

    Entrées :
      sensor.jarvis_signals  — signaux directs (signal_reader.py)
      sensor.jarvis_vectors  — sorties + sorties_derivees (device_map_reader.py)
      sensor.jarvis_presence — liste des personnes à domicile (presence.py)

    Sortie :
      sensor.jarvis_gemma_etats — attributs JSON, une clé par personne
        {
          "Jean":  { "etat": "ACTIF_BUREAU", "score": 0.91, "tous_scores": {...} },
          "Marie": { "etat": "REPOS_TV",     "score": 0.87, "tous_scores": {...} }
        }

      gemma_state.json — même contenu sur disque (consommé par moteur_scoring)

    Principe :
      Le snapshot (signaux physiques) est commun à toute la maison.
      Chaque personne présente reçoit sa propre évaluation indépendante.
      Les personnes absentes ne sont pas évaluées.
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
        self.gemma_snapshot_debug_path = self.args.get(
            "gemma_snapshot_debug_path",
            "/homeassistant/gemma_snapshot_debug.json"
        )
        self.entity_gemma_etats = self.args.get(
            "entity_gemma_etats",
            "sensor.jarvis_gemma_etats"
        )

        self.poids = self._load_poids()
        self.etats_precedents = {}   # { "Jean": "ACTIF_BUREAU", ... }

        # Évaluation toutes les 30 secondes
        self.run_every(self._evaluer, "now", 30)

        # Dump snapshot debug toutes les 5 minutes
        self.run_every(self._dump_snapshot_debug, "now", 300)

        self.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        self.log("  Moteur GEMMA v3.0 — multi-humain")
        self.log(f"  poids.yaml   : {self.poids_path}")
        self.log(f"  etats chargés: {list(self.poids.get('etats', {}).keys())}")
        self.log(f"  sortie       : {self.entity_gemma_etats}")
        self.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # ─────────────────────────────────────────────
    # Chargement poids
    # ─────────────────────────────────────────────

    def _load_poids(self):
        if not os.path.exists(self.poids_path):
            self.log(f"poids.yaml introuvable : {self.poids_path}", level="ERROR")
            return {}
        with open(self.poids_path, "r") as f:
            return yaml.safe_load(f) or {}

    # ─────────────────────────────────────────────
    # Lecture personnes présentes
    # ─────────────────────────────────────────────

    def _lire_personnes_home(self):
        """
        Retourne la liste des prénoms à domicile depuis sensor.jarvis_presence.
        Ex: ["Jean", "Marie"]
        Retourne [] si la maison est vide ou en cas d'erreur.
        """
        try:
            raw   = self.get_state("sensor.jarvis_presence", attribute="all") or {}
            attrs = raw.get("attributes", {})
            return attrs.get("personnes_home", [])
        except Exception as e:
            self.log(f"[PRÉSENCE] Erreur lecture : {e}", level="WARNING")
            return []

    # ─────────────────────────────────────────────
    # Lecture snapshot signaux
    # ─────────────────────────────────────────────

    def _load_snapshot(self):
        """
        Reconstruit le dict signals depuis les deux entités HA.
        Le snapshot est commun — il représente l'état physique de la maison entière.
        """
        signals = {}

        # 1. Signaux directs (signal_reader.py)
        try:
            raw = self.get_state("sensor.jarvis_signals", attribute="all") or {}
            signals.update(raw.get("attributes", {}))
        except Exception as e:
            self.log(f"Erreur lecture sensor.jarvis_signals : {e}", level="WARNING")

        # 2. Vectors (device_map_reader.py)
        try:
            raw     = self.get_state("sensor.jarvis_vectors", attribute="all") or {}
            vectors = raw.get("attributes", {})

            for device_id, vecteur in vectors.items():
                if not isinstance(vecteur, dict):
                    continue

                piece = vecteur.get("piece")

                for cle, val in vecteur.get("sorties", {}).items():
                    signals[f"{device_id}_{cle}"] = {
                        "value": self._normaliser_bool(val),
                        "piece": piece,
                        "type":  "sortie",
                    }

                for cle, val in vecteur.get("sorties_derivees", {}).items():
                    signals[f"{device_id}_{cle}"] = {
                        "value": self._normaliser_bool(val),
                        "piece": piece,
                        "type":  "sortie_derivee",
                    }

        except Exception as e:
            self.log(f"Erreur lecture sensor.jarvis_vectors : {e}", level="WARNING")

        return signals

    # ─────────────────────────────────────────────
    # Normalisation string → bool
    # ─────────────────────────────────────────────

    def _normaliser_bool(self, val):
        if isinstance(val, str):
            if val.lower() == "true":
                return True
            if val.lower() == "false":
                return False
        return val

    # ─────────────────────────────────────────────
    # Évaluation principale
    # ─────────────────────────────────────────────

    def _evaluer(self, kwargs=None):
        personnes_home = self._lire_personnes_home()

        if not personnes_home:
            self.log("[GEMMA] Maison vide — évaluation suspendue")
            self._publier({})
            return

        signals = self._load_snapshot()
        if not signals:
            self.log("[GEMMA] Snapshot vide — évaluation suspendue", level="WARNING")
            return

        heure  = datetime.now().hour
        modif  = self._modificateur_contexte(heure)
        seuil  = self.poids.get("seuil", 0.75)
        etats  = self.poids.get("etats", {})

        resultats = {}

        for personne in personnes_home:
            scores = {}
            for nom_etat, config in etats.items():
                score = self._calculer_score(signals, config, modif)
                scores[nom_etat] = round(score, 3)

            scores_tries    = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            meilleur_etat, meilleur_score = scores_tries[0]
            etat_final      = meilleur_etat if meilleur_score >= seuil else "INDETERMINE"

            resultats[personne] = {
                "etat":        etat_final,
                "score":       meilleur_score,
                "tous_scores": scores,
            }

            # Log uniquement si changement d'état
            precedent = self.etats_precedents.get(personne)
            if etat_final != precedent:
                self.log(f"[GEMMA] {personne} : {precedent!r} → {etat_final!r} (score={meilleur_score})")
                self.etats_precedents[personne] = etat_final

        self._publier(resultats, heure, modif)

    # ─────────────────────────────────────────────
    # Calcul du score d'un état
    # ─────────────────────────────────────────────

    def _calculer_score(self, signals, config_etat, modif_contexte):
        signaux_config = config_etat.get("signaux", {})
        if not signaux_config:
            return 0.0

        score_total = 0.0
        poids_total = 0.0

        for signal_id, params in signaux_config.items():
            poids        = params.get("poids", 0.5)
            poids_total += abs(poids)

            valeur = self._lire_valeur(signals, signal_id)
            if valeur is None:
                continue

            contribution  = self._evaluer_signal(valeur, params) * poids
            score_total  += contribution

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
            fin   = cfg.get("fin",   24)
            modif = cfg.get("modificateur", 1.0)
            if debut > fin:
                if heure >= debut or heure < fin:
                    return modif
            else:
                if debut <= heure < fin:
                    return modif
        return 1.0

    # ─────────────────────────────────────────────
    # Publication
    # ─────────────────────────────────────────────

    def _publier(self, resultats, heure=None, modif=None):
        """
        Publie les N états dans sensor.jarvis_gemma_etats (attributs)
        et dans gemma_state.json (consommé par moteur_scoring).

        Format gemma_state.json :
        {
          "generated_at": "2026-05-27T14:32:00",
          "heure": 14,
          "modificateur": 1.0,
          "personnes": {
            "Jean":  { "etat": "ACTIF_BUREAU", "score": 0.91, "tous_scores": {...} },
            "Marie": { "etat": "REPOS_TV",     "score": 0.87, "tous_scores": {...} }
          }
        }
        """
        maintenant = datetime.now().isoformat(timespec="seconds")

        sortie = {
            "generated_at": maintenant,
            "heure":        heure,
            "modificateur": modif,
            "personnes":    resultats,
        }

        # Écriture disque
        try:
            with open(self.gemma_state_path, "w") as f:
                json.dump(sortie, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"Erreur écriture gemma_state.json : {e}", level="ERROR")

        # Publication HA — un attribut par personne
        try:
            attrs = {
                personne: {
                    "etat":  data["etat"],
                    "score": data["score"],
                }
                for personne, data in resultats.items()
            }
            attrs["updated_at"]    = maintenant
            attrs["friendly_name"] = "GEMMA — États par personne"

            self.set_state(
                self.entity_gemma_etats,
                state=str(len(resultats)),   # nb de personnes évaluées
                attributes=attrs,
            )
        except Exception as e:
            self.log(f"Erreur mise à jour {self.entity_gemma_etats} : {e}", level="WARNING")

    # ─────────────────────────────────────────────
    # Dump snapshot debug toutes les 5min
    # ─────────────────────────────────────────────

    def _dump_snapshot_debug(self, kwargs=None):
        signals    = self._load_snapshot()
        etats      = self.poids.get("etats", {})
        personnes  = self._lire_personnes_home()

        analyse = {}
        for nom_etat, config in etats.items():
            signaux_config = config.get("signaux", {})
            detail = {}
            for signal_id, params in signaux_config.items():
                entry = signals.get(signal_id)
                detail[signal_id] = {
                    "poids":           params.get("poids"),
                    "valeur_active":   params.get("valeur_active", "—"),
                    "valeur_snapshot": entry.get("value") if entry else "ABSENT",
                    "present":         entry is not None,
                }
            analyse[nom_etat] = detail

        sortie = {
            "generated_at":     datetime.now().isoformat(timespec="seconds"),
            "personnes_home":   personnes,
            "snapshot_keys":    sorted(signals.keys()),
            "analyse_par_etat": analyse,
        }

        try:
            with open(self.gemma_snapshot_debug_path, "w") as f:
                json.dump(sortie, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"Erreur écriture snapshot debug : {e}", level="ERROR")
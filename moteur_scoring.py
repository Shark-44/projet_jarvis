import appdaemon.plugins.hass.hassapi as hass
import yaml
import json
import os
from datetime import datetime


class MoteurScoring(hass.Hass):
    """
    Moteur de scoring — v2.0 multi-humain
    ======================================
    Consomme les N états produits par moteur_gemma.py (un par personne)
    et applique les comportements lumineux pièce par pièce.

    Entrées :
      sensor.jarvis_gemma_etats — attributs { "Jean": {...}, "Marie": {...} }
      gemma_state.json          — même données sur disque (fallback)
      weather.frossay + sun.sun — condition météo

    Logique :
      Pour chaque personne → etat × meteo → actions dans comportements.yaml
      Les actionneurs sont filtrés par pièce :
        si Jean est en ACTIF_BUREAU, seul Eclairage_bureau est touché
        si Marie est en REPOS_TV, seul Wled_Salon etc. est touché
      Conflit de pièce (deux personnes même pièce) → état prioritaire (score le plus haut)
    """

    def initialize(self):
        self.comportements_path = self.args.get(
            "comportements_path",
            "/homeassistant/comportements.yaml"
        )
        self.gemma_state_path = self.args.get(
            "gemma_state_path",
            "/homeassistant/gemma_state.json"
        )
        self.entity_meteo = self.args.get(
            "entity_meteo",
            "weather.frossay"
        )
        self.entity_sun = self.args.get(
            "entity_sun",
            "sun.sun"
        )
        self.temp_seuil_chaud = self.args.get(
            "temp_seuil_chaud", 22
        )
        self.index_path = self.args.get(
            "index_path", "/homeassistant/index.yaml"
        )
        self.entity_gemma_etats = self.args.get(
            "entity_gemma_etats",
            "sensor.jarvis_gemma_etats"
        )

        self.comportements    = self._load_comportements()
        self.meteo_precedente = None

        # Déclencheur : changement d'état GEMMA (nb de personnes évaluées change)
        self.listen_state(self._on_gemma_change, self.entity_gemma_etats, attribute="all")
        # Déclencheur : changement météo ou soleil
        self.listen_state(self._on_meteo_change, self.entity_meteo)
        self.listen_state(self._on_meteo_change, self.entity_sun)

        self.log("=" * 40)
        self.log("  MoteurScoring v2.0  multi-humain")
        self.log(f"  comportements : {self.comportements_path}")
        self.log(f"  etats charges : {list(self.comportements.get('etats', {}).keys())}")
        self.log(f"  gemma_etats   : {self.entity_gemma_etats}")
        self.log("=" * 40)

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
        self.log(f"[DECLENCHEUR] GEMMA etats mis a jour")
        self._appliquer()

    def _on_meteo_change(self, entity, attribute, old, new, kwargs):
        meteo = self._lire_condition_meteo()
        if meteo != self.meteo_precedente:
            self.log(f"[DECLENCHEUR] Météo : {self.meteo_precedente!r} → {meteo!r}")
            self.meteo_precedente = meteo
            self._appliquer(meteo=meteo)

    # ─────────────────────────────────────────────
    # Lecture présence
    # ─────────────────────────────────────────────

    def _lire_presence(self):
        try:
            raw   = self.get_state("sensor.jarvis_presence", attribute="all") or {}
            attrs = raw.get("attributes", {})
            if attrs:
                return attrs
        except Exception as e:
            self.log(f"[PRÉSENCE] Erreur lecture : {e}", level="WARNING")
        return {"maison_occupee": True, "personnes_home": []}

    # ─────────────────────────────────────────────
    # Lecture états GEMMA (N personnes)
    # ─────────────────────────────────────────────

    def _lire_etats_gemma(self):
        """
        Retourne le dict des états par personne depuis gemma_state.json.
        Format retourné :
          {
            "Jean":  { "etat": "ACTIF_BUREAU", "score": 0.91 },
            "Marie": { "etat": "REPOS_TV",     "score": 0.87 }
          }
        """
        try:
            if os.path.exists(self.gemma_state_path):
                with open(self.gemma_state_path, "r") as f:
                    data = json.load(f)
                    return data.get("personnes", {})
        except Exception as e:
            self.log(f"[GEMMA] Erreur lecture gemma_state.json : {e}", level="ERROR")
        return {}

    # ─────────────────────────────────────────────
    # Lecture météo
    # ─────────────────────────────────────────────

    def _lire_condition_meteo(self):
        """
        Détermine l'état environnemental réel et local de la maison
        en combinant le soleil et les capteurs physiques purifiés de Jarvis.
        """
        try:
            # 1. Priorité absolue : La nuit astronomique
            sun_state = self.get_state(self.entity_sun)
            if sun_state == "below_horizon":
                return "nuit"

            # 2. Récupération des capteurs physiques locaux (Meteo_locale)
            vectors_attrs = self.get_state("sensor.jarvis_vectors", attribute="all") or {}
            meteo_locale = vectors_attrs.get("attributes", {}).get("Meteo_locale", {})
            
            sorties_derivees = meteo_locale.get("sorties_derivees", {})
            is_pleut_local = sorties_derivees.get("pleut", False)
            besoin_lumiere = sorties_derivees.get("besoin_lumiere", False)

            # Normalisation en chaînes si besoin
            pleut = True if is_pleut_local is True or str(is_pleut_local).lower() == "true" else False
            sombre = True if besoin_lumiere is True or str(besoin_lumiere).lower() == "true" else False

            # 3. Logique d'adaptation environnementale locale
            if pleut:
                return "pluie"       # Il pleut localement (vérifié par le capteur ext)
            if sombre:
                return "sombre"      # Le ciel s'est assombri (vérifié par le luxmètre)
            
            return "soleil"          # Il fait jour et lumineux

        except Exception as e:
            self.log(f"[MÉTÉO LOCAL] Erreur calcul environnement : {e}", level="WARNING")
            return "soleil" # Fallback sécurisé

    # ─────────────────────────────────────────────
    # Application des comportements
    # ─────────────────────────────────────────────

    def _appliquer(self, meteo=None):
        now = datetime.now().strftime("%H:%M:%S")
        self.log(f"**** _appliquer() [{now}] ***********")

        # ── Guard 1 : présence ──────────────────
        presence       = self._lire_presence()
        maison_occupee = presence.get("maison_occupee", True)
        personnes_home = presence.get("personnes_home", [])
        self.log(f" PRESENCE  : maison_occupee={maison_occupee}  personnes={personnes_home}")

        if not maison_occupee:
            self.log(" Maison vide — scoring suspendu")
            self.log(f"╚{'═'*50}")
            return

        # ── Guard 2 : météo ─────────────────────
        if not meteo:
            meteo = self._lire_condition_meteo()
        self.log(f" METEO     : {meteo!r}")

        # ── Lecture états GEMMA ──────────────────
        etats_personnes = self._lire_etats_gemma()
        if not etats_personnes:
            self.log("  Aucun état GEMMA disponible")
            self.log(f"╚{'═'*50}")
            return

        # ── Résolution conflits de pièce ─────────
        # Si deux personnes ont des états qui touchent les mêmes actionneurs,
        # la personne avec le score le plus élevé gagne pour cette pièce.
        etats_config   = self.comportements.get("etats", {})
        actions_finales = {}   # { actionneur_id: valeur }
        scores_finaux   = {}   # { actionneur_id: score } pour arbitrage

        for personne, data in etats_personnes.items():
            etat  = data.get("etat")
            score = data.get("score", 0.0)

            self.log(f" {personne:10s} : etat={etat!r}  score={score}")

            if not etat or etat == "INDETERMINE":
                self.log(f"  Etat {etat!r} = ignore")
                continue

            config_etat = etats_config.get(etat)
            if not config_etat:
                self.log(f"  Aucun comportement pour {etat!r}")
                continue

            actions = config_etat.get(meteo)
            if not actions:
                self.log(f"  Aucune action pour {etat!r} × {meteo!r}")
                continue

            self.log(f" {etat} × {meteo} → {len(actions)} actionneur(s)")

            for actionneur_id, valeur in actions.items():
                score_existant = scores_finaux.get(actionneur_id, -1)
                if score > score_existant:
                    actions_finales[actionneur_id] = valeur
                    scores_finaux[actionneur_id]   = score
                else:
                    self.log(f" Conflit {actionneur_id!r} — score {score} < {score_existant}, ignore")

        # ── Application ──────────────────────────
        if not actions_finales:
            self.log("Aucune action a appliquer")
            self.log(f"╚{'═'*50}")
            return

        self.log(f" Application de {len(actions_finales)} actionneur(s)")
        for actionneur_id, valeur in actions_finales.items():
            entity  = self._id_vers_entity(actionneur_id)
            if not entity:
                self.log(f" {actionneur_id!r} → entité introuvable (vérifier index.yaml)")
                continue

            resultat = self._actionner_log(entity, valeur)
            self.log(f"║   {'✅' if resultat else '❌'} {actionneur_id!r} → {entity}  valeur={valeur}")

        self.log(f"╚{'═'*50}")

    # ─────────────────────────────────────────────
    # Actionnement
    # ─────────────────────────────────────────────

    def _actionner_log(self, entity, valeur):
        try:
            if isinstance(valeur, bool):
                if valeur:
                    self.turn_on(entity)
                else:
                    self.turn_off(entity)
            elif isinstance(valeur, (int, float)):
                if valeur == 0:
                    self.turn_off(entity)
                else:
                    self.turn_on(entity, brightness=int(valeur))
            return True
        except Exception as e:
            self.log(f"[ACTION] Erreur sur {entity} : {e}", level="ERROR")
            return False

    # ─────────────────────────────────────────────
    # Résolution id → entity_id HA
    # ─────────────────────────────────────────────

    def _id_vers_entity(self, actionneur_id):
        try:
            raw     = self.get_state("sensor.jarvis_signals", attribute="all") or {}
            signals = raw.get("attributes", {})
            if actionneur_id in signals:
                return signals[actionneur_id].get("entity")
        except Exception:
            pass

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
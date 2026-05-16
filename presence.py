import appdaemon.plugins.hass.hassapi as hass
import json
import os
from datetime import datetime


class Presence(hass.Hass):
    """
    Capteur de présence humaine — couche GPS/réseau.
    Complémentaire aux capteurs physiques (FP2, LD2450).

    Rôle :
      - Écoute les device_trackers via PersonRepository
      - Détecte arrivées et départs
      - Écrit snapshot["presence"] à chaque transition
      - Délègue à eteindre_maison si la maison devient vide

    Principe directeur :
      Presence est un capteur comme FP2 — il alimente snapshot.json.
      moteur_scoring consomme snapshot sans savoir d'où vient l'info.
    """

    def initialize(self):
        self.persons  = self.get_app("person_repository")
        self.eteindre = self.get_app("eteindre_maison")

        self.snapshot_path = self.args.get(
            "snapshot_path",
            "/config/appdaemon/apps/snapshot.json"
        )

        # Écriture initiale au démarrage
        self._write_snapshot_presence()

        # Abonnement aux trackers de toutes les personnes connues
        for nom in self._get_all_persons():
            tracker = self.persons.get_tracker(nom)
            self.listen_state(self.on_arrive, tracker, new="home")
            self.listen_state(
                self.on_part, tracker,
                new="not_home", old="home",
                duration=30
            )

        self.log("Presence initialisé — trackers abonnés")

    # ─────────────────────────────────────────────
    # Arrivée
    # ─────────────────────────────────────────────

    def on_arrive(self, entity, attribute, old, new, kwargs):
        nom = self._get_nom_by_tracker(entity)
        self.log(f"Maison OCCUPÉE — {nom} est arrivé")
        self._write_snapshot_presence()

    # ─────────────────────────────────────────────
    # Départ
    # ─────────────────────────────────────────────

    def on_part(self, entity, attribute, old, new, kwargs):
        nom = self._get_nom_by_tracker(entity)
        self.log(f"{nom} a quitté la maison")

        self._write_snapshot_presence()

        if len(self.persons.get_all_home()) == 0:
            self.log("Maison VIDE — délégation à eteindre_maison")
            self.eteindre.dernier_parti(nom)

    # ─────────────────────────────────────────────
    # Écriture snapshot["presence"]
    # ─────────────────────────────────────────────

    def _write_snapshot_presence(self):
        """
        Écrit le bloc presence dans snapshot.json de façon atomique.
        Ne touche pas aux autres blocs (signals, vectors).
        """
        home = self.persons.get_all_home()

        # Lecture snapshot existant
        snapshot = {}
        try:
            if os.path.exists(self.snapshot_path):
                with open(self.snapshot_path, "r") as f:
                    snapshot = json.load(f)
        except Exception as e:
            self.log(f"Lecture snapshot échouée : {e} — réécriture complète", level="WARNING")

        snapshot["presence"] = {
            "maison_occupee": len(home) > 0,
            "personnes_home": home,
            "derniere_maj": datetime.now().isoformat()
        }

        # Écriture atomique : tmp + os.replace évite la corruption
        tmp_path = self.snapshot_path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.snapshot_path)
            self.log(f"snapshot[presence] mis à jour — home: {home}")
        except Exception as e:
            self.log(f"Erreur écriture snapshot presence : {e}", level="ERROR")

    # ─────────────────────────────────────────────
    # Utilitaires
    # ─────────────────────────────────────────────

    def _get_all_persons(self):
        return self.persons.get_family() + self.persons.get_invites()

    def _get_nom_by_tracker(self, entity):
        for nom in self._get_all_persons():
            if self.persons.get_tracker(nom) == entity:
                return nom
        return entity  # fallback : retourne l'entity_id brut

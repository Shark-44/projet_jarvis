import appdaemon.plugins.hass.hassapi as hass

class Presence(hass.Hass):

    def initialize(self):
        self.persons = self.get_app("person_repository")
        self.eteindre = self.get_app("eteindre_maison")

        for nom in self._get_all_persons():
            tracker = self.persons.get_tracker(nom)
            self.listen_state(self.on_arrive, tracker, new="home")
            self.listen_state(self.on_part, tracker, new="not_home", old="home", duration=30)

    # ─────────────────────────────────────────────
    # Arrivée
    # ─────────────────────────────────────────────
    def on_arrive(self, entity, attribute, old, new, kwargs):
        nom = self._get_nom_by_tracker(entity)
        self.log(f"Maison OCCUPÉE — {nom} est arrivé")

    # ─────────────────────────────────────────────
    # Départ
    # ─────────────────────────────────────────────
    def on_part(self, entity, attribute, old, new, kwargs):
        nom = self._get_nom_by_tracker(entity)
        self.log(f"{nom} a quitté la maison")

        if len(self.persons.get_all_home()) == 0:
            self.log("Maison VIDE — délégation à eteindre_maison")
            self.eteindre.dernier_parti(nom)

    # ─────────────────────────────────────────────
    # Utilitaires
    # ─────────────────────────────────────────────
    def _get_all_persons(self):
        return self.persons.get_family() + self.persons.get_invites()

    def _get_nom_by_tracker(self, entity):
        for nom in self._get_all_persons():
            if self.persons.get_tracker(nom) == entity:
                return nom
        return entity
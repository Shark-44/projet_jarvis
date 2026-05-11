import appdaemon.plugins.hass.hassapi as hass
import yaml

class PersonRepository(hass.Hass):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._path = "/homeassistant/persons.yaml"

    def initialize(self):
        pass

    def _load(self):
        with open(self._path, "r") as f:
            return yaml.safe_load(f)

    def get_tracker(self, nom):
        data = self._load()
        return data["personnes"][nom]["tracker"]

    def get_notify(self, nom):
        data = self._load()
        return data["personnes"][nom]["notify"]

    def is_home(self, nom):
        tracker = self.get_tracker(nom)
        return self.get_state(tracker) == "home"

    def get_family(self):
        data = self._load()
        return [nom for nom, p in data["personnes"].items() if p["role"] == "family"]

    def get_invites(self):
        data = self._load()
        return [nom for nom, p in data["personnes"].items() if p["role"] == "invite"]

    def notify_family(self, message):
        for nom in self.get_family():
            self.call_service(self.get_notify(nom), message=message)

    def get_all_home(self):
        data = self._load()
        return [nom for nom in data["personnes"] if self.is_home(nom)]

    def notify_all_home(self, message):
        for nom in self.get_all_home():
            self.call_service(self.get_notify(nom), message=message)
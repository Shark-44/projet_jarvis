import os
from datetime import datetime
import yaml
import appdaemon.plugins.hass.hassapi as hass


class DeviceMapReader(hass.Hass):
    """
    Lecteur vectoriel — Complément de signal_reader.py
    Lit device_map.yaml (structure: triggers) et produit sensor.jarvis_vectors.
    """

    def initialize(self):
        self.device_map_path = self.args.get(
            "device_map_path",
            "/homeassistant/device_map.yaml"
        )
        self._hysterese_cache = {}
        
        # Cache local pour centraliser les états et éviter les conflits d'écriture
        self._vectors_cache = {}

        self.device_map = self._load_device_map()
        if not self.device_map:
            self.log("device_map.yaml vide ou introuvable — abandon", level="ERROR")
            return

        self._subscribe_all()
        self._take_snapshot()
        self.log("DeviceMapReader initialisé — abonnements actifs")

    # ─────────────────────────────────────────────
    # Chargement du fichier YAML
    # ─────────────────────────────────────────────

    def _load_device_map(self):
        if not os.path.exists(self.device_map_path):
            self.log(f"Introuvable : {self.device_map_path}", level="ERROR")
            return {}
        try:
            with open(self.device_map_path, "r") as f:
                data = yaml.safe_load(f) or {}
            self.log(f"device_map chargé — pièces : {list(data.get('pieces', {}).keys())}")
            return data
        except Exception as e:
            self.log(f"Erreur lors de la lecture du fichier YAML : {e}", level="ERROR")
            return {}

    # ─────────────────────────────────────────────
    # Abonnements dynamiques aux entités HA
    # ─────────────────────────────────────────────

    def _subscribe_all(self):
        count = 0
        for piece, contenu in self.device_map.get("pieces", {}).items():
            for device in contenu.get("recepteurs", []):
                device_id = device.get("id")
                for cle, entity_id in device.get("triggers", {}).items():
                    self.listen_state(
                        self._on_change,
                        entity_id,
                        device_id=device_id,
                        piece=piece,
                        device=device,
                    )
                    count += 1
        self.log(f"Abonnements créés : {count} entités")

    # ─────────────────────────────────────────────
    # Handler d'événement (Changement d'état)
    # ─────────────────────────────────────────────

    def _on_change(self, entity, attribute, old, new, kwargs):
        device_id = kwargs.get("device_id")
        piece     = kwargs.get("piece")
        device    = kwargs.get("device")
        self._build_and_write_snapshot(device_id, piece, device)

    # ─────────────────────────────────────────────
    # Snapshot initial au démarrage
    # ─────────────────────────────────────────────

    def _take_snapshot(self):
        for piece, contenu in self.device_map.get("pieces", {}).items():
            for device in contenu.get("recepteurs", []):
                device_id = device.get("id")
                self._build_and_write_snapshot(device_id, piece, device)

    # ─────────────────────────────────────────────
    # Construction de l'objet JSON / YAML pour un Device
    # ─────────────────────────────────────────────

    def _build_and_write_snapshot(self, device_id, piece, device):
        seuils   = device.get("seuils", {})
        triggers = device.get("triggers", {})

        snapshot_device = {
            "piece":            piece,
            "timestamp":        datetime.now().isoformat(),
            "sorties":          {},
            "sorties_derivees": {},
            "vecteurs":         {},
        }

        # 1. Lecture brute de toutes les entités configurées
        raw_values = {}
        for cle, entity_id in triggers.items():
            try:
                raw_values[cle] = self.get_state(entity_id)
            except Exception as e:
                self.log(f"Erreur lecture {entity_id} : {e}", level="WARNING")
                raw_values[cle] = None

        # 2. Tri et traitement des données (avec valeurs de repli pour ESPHome)
        for cle, raw in raw_values.items():
            
            # --- CAS A : Capteurs de présence / mouvement (binary_sensor) ---
            if "presence" in cle or "mouvement" in cle or "immobile" in cle:
                if raw in (None, "unavailable", "unknown"):
                    snapshot_device["sorties"][cle] = False  # Par défaut : pas d'occupation
                else:
                    snapshot_device["sorties"][cle] = self._to_bool(raw)

            # --- CAS B : Calculs météo / lumière (Sorties dérivées) ---
            elif cle == "pluie_mv":
                snapshot_device["sorties_derivees"]["pleut"] = self._calc_pleut(raw, seuils)

            elif cle == "lumiere_mv":
                snapshot_device["sorties_derivees"]["besoin_lumiere"] = self._calc_besoin_lumiere(
                    device_id, raw, seuils
                )

            # --- CAS C : Nombre de cibles (Entiers) ---
            elif "nb_cibles" in cle or "nombre" in cle:
                if raw in (None, "unavailable", "unknown"):
                    snapshot_device["vecteurs"][cle] = 0
                else:
                    snapshot_device["vecteurs"][cle] = self._to_int(raw)

            # --- CAS D : Vitesses du radar (Flottants) ---
            elif "vitesse" in cle:
                if raw in (None, "unavailable", "unknown"):
                    snapshot_device["vecteurs"][cle] = 0.0
                else:
                    snapshot_device["vecteurs"][cle] = self._to_vitesse(raw, seuils)

            # --- CAS E : Directions de cibles ou états textuels (Chaînes) ---
            elif "direction" in cle or "etat" in cle:
                if raw in (None, "unavailable", "unknown"):
                    snapshot_device["vecteurs"][cle] = "unknown"
                else:
                    snapshot_device["vecteurs"][cle] = str(raw)

            # --- CAS F : Autres capteurs numériques génériques ---
            else:
                if raw in (None, "unavailable", "unknown"):
                    snapshot_device["vecteurs"][cle] = None
                else:
                    snapshot_device["vecteurs"][cle] = self._to_float(raw)

        self._write_snapshot(device_id, snapshot_device)

    # ─────────────────────────────────────────────
    # Logique de calcul interne
    # ─────────────────────────────────────────────

    def _calc_pleut(self, raw, seuils):
        try:
            return float(raw) > float(seuils.get("pluie_seuil_mv", 200))
        except (TypeError, ValueError):
            return False

    def _calc_besoin_lumiere(self, device_id, raw, seuils):
        try:
            lux       = float(raw) * float(seuils.get("lux_conversion", 0.02276))
            seuil     = float(seuils.get("lux_seuil", 50))
            hysterese = float(seuils.get("lux_hysterese", 10))
            cache_key = f"{device_id}_besoin_lumiere"
            etat      = self._hysterese_cache.get(cache_key)

            if etat is None:
                nouvel_etat = lux < seuil
            elif etat:
                nouvel_etat = lux < (seuil + hysterese)
            else:
                nouvel_etat = lux < (seuil - hysterese)

            self._hysterese_cache[cache_key] = nouvel_etat
            return nouvel_etat
        except (TypeError, ValueError):
            return False

    # ─────────────────────────────────────────────
    # Convertisseurs de types sécurisés
    # ─────────────────────────────────────────────

    def _to_bool(self, val):
        if val in (True, "on", "ON", "true", "True", "home"):
            return True
        if val in (False, "off", "OFF", "false", "False", "not_home"):
            return False
        return False

    def _to_int(self, raw):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return 0

    def _to_float(self, raw):
        try:
            return round(float(raw), 3)
        except (TypeError, ValueError):
            return 0.0

    def _to_vitesse(self, raw, seuils):
        try:
            v = float(raw)
            seuil_silence = float(seuils.get("vitesse_silence", 0))
            return 0.0 if v < seuil_silence else round(v, 2)
        except (TypeError, ValueError):
            return 0.0

    # ─────────────────────────────────────────────
    # Écriture finale dans Home Assistant
    # ─────────────────────────────────────────────

    def _write_snapshot(self, device_id, snapshot_device):
        # On enregistre dans notre dictionnaire local global
        self._vectors_cache[device_id] = {
            "piece":            snapshot_device["piece"],
            "timestamp":        snapshot_device["timestamp"],
            "sorties":          dict(snapshot_device["sorties"]),
            "sorties_derivees": dict(snapshot_device["sorties_derivees"]),
            "vecteurs":         dict(snapshot_device["vecteurs"]),
        }

        try:
            # Envoi global de la structure à Home Assistant
            self.set_state(
                "sensor.jarvis_vectors",
                state="ok",
                attributes={
                **self._vectors_cache,
                "unique_id": "jarvis_vectors_sensor",  
            },
            )
        except Exception as e:
            self.log(f"Erreur écriture sensor.jarvis_vectors : {e}", level="ERROR")

    # ─────────────────────────────────────────────
    # API publique utilisable par d'autres applications AppDaemon
    # ─────────────────────────────────────────────

    def get_vectors(self):
        return self._vectors_cache

    def get_device(self, device_id):
        return self._vectors_cache.get(device_id)
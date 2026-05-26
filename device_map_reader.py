import os
from datetime import datetime
import yaml
import appdaemon.plugins.hass.hassapi as hass
from vector_engine import RoomVectorEngine


class DeviceMapReader(hass.Hass):
    """
    Lecteur de devices multi-entités — Complément de signal_reader.py

    signal_reader     : 1 device = 1 entité binaire
    device_map_reader : 1 device = N entités + vecteurs temporels

    Produit sensor.jarvis_vectors avec pour chaque device :
      - sorties          : valeurs binaires directes
      - sorties_derivees : valeurs calculées (pleut, besoin_lumiere, en_lecture,
                           immobile, toutes_statiques, mouvement_local...)
      - vecteurs         : données brutes radar (non traitées)

    Nommage des signaux dans le snapshot GEMMA :
      {device_id}_{cle_sortie}
      ex : Presence_chambre_presence_piece
           Presence_chambre_toutes_statiques
           Meteo_locale_besoin_lumiere
           Meteo_locale_pleut
           Tele_en_lecture
    """

    def initialize(self):
        # ── Vector engines par pièce LD2450 ───────────────────────
        # piece_id = préfixe des clés dans le snapshot
        # doit correspondre au device_id dans device_map.yaml
        self.vector_engines = {
            "Presence_chambre": RoomVectorEngine(
                piece_id="Presence_chambre",
                observation_mode=True
            ),
            "Presence_bureau": RoomVectorEngine(
                piece_id="Presence_bureau",
                observation_mode=True
            ),
        }

        self.device_map_path = self.args.get(
            "device_map_path",
            "/homeassistant/device_map.yaml"
        )
        self._hysterese_cache = {}
        self._vectors_cache   = {}

        self.device_map = self._load_device_map()
        if not self.device_map:
            self.log("device_map.yaml vide ou introuvable — abandon", level="ERROR")
            return

        self._subscribe_all()
        self._take_snapshot()
        self.log("DeviceMapReader initialise — abonnements actifs")

    # ─────────────────────────────────────────────
    # Chargement YAML
    # ─────────────────────────────────────────────

    def _load_device_map(self):
        if not os.path.exists(self.device_map_path):
            self.log(f"Introuvable : {self.device_map_path}", level="ERROR")
            return {}
        try:
            with open(self.device_map_path, "r") as f:
                data = yaml.safe_load(f) or {}
            self.log(f"device_map charge — pièces : {list(data.get('pieces', {}).keys())}")
            return data
        except Exception as e:
            self.log(f"Erreur lecture YAML : {e}", level="ERROR")
            return {}

    # ─────────────────────────────────────────────
    # Itérateur sur tous les devices
    # ─────────────────────────────────────────────

    def _iter_devices(self):
        """Génère (piece, device) pour chaque device de toutes les pièces."""
        for piece, contenu in self.device_map.get("pieces", {}).items():
            for device in contenu.get("recepteurs", []):
                yield piece, device

    # ─────────────────────────────────────────────
    # Abonnements dynamiques
    # ─────────────────────────────────────────────

    def _subscribe_all(self):
        count = 0
        for piece, device in self._iter_devices():
            device_id = device.get("id")

            # Sorties directes — entités HA réelles uniquement
            for cle, entity_id in device.get("sorties", {}).items():
                if isinstance(entity_id, str) and entity_id.startswith(
                    ("binary_sensor.", "media_player.", "sensor.", "switch.", "light.")
                ):
                    self.listen_state(
                        self._on_change, entity_id,
                        device_id=device_id, piece=piece, device=device,
                    )
                    count += 1

            # Mesures brutes — alimentent les sorties dérivées
            for cle, entity_id in device.get("mesures", {}).items():
                if isinstance(entity_id, str) and entity_id.startswith(
                    ("binary_sensor.", "media_player.", "sensor.", "switch.", "light.")
                ):
                    self.listen_state(
                        self._on_change, entity_id,
                        device_id=device_id, piece=piece, device=device,
                    )
                    count += 1

            # Vecteurs — alimentent vector_engine
            for cle, entity_id in device.get("vecteurs", {}).items():
                if isinstance(entity_id, str) and entity_id.startswith(
                    ("binary_sensor.", "media_player.", "sensor.", "switch.", "light.")
                ):
                    self.listen_state(
                        self._on_change, entity_id,
                        device_id=device_id, piece=piece, device=device,
                    )
                    count += 1

        self.log(f"Abonnements crees : {count} entites")

    def _on_change(self, entity, attribute, old, new, kwargs):
        device_id = kwargs.get("device_id")
        piece     = kwargs.get("piece")
        device    = kwargs.get("device")
        self._build_and_write_snapshot(device_id, piece, device)

    # ─────────────────────────────────────────────
    # Snapshot initial
    # ─────────────────────────────────────────────

    def _take_snapshot(self):
        for piece, device in self._iter_devices():
            device_id = device.get("id")
            self._build_and_write_snapshot(device_id, piece, device)

    # ─────────────────────────────────────────────
    # Construction snapshot d'un device
    # ─────────────────────────────────────────────

    def _build_and_write_snapshot(self, device_id, piece, device):
        seuils = device.get("seuils", {})

        snapshot_device = {
            "piece":            piece,
            "timestamp":        datetime.now().isoformat(),
            "sorties":          {},
            "sorties_derivees": {},
            "vecteurs":         {},
        }

        # ── 1. Sorties directes ──────────────────────────────────────
        # Entités HA binaires — lecture directe
        for cle, entity_id in device.get("sorties", {}).items():
            if not isinstance(entity_id, str):
                continue
            try:
                raw = self.get_state(entity_id)
                snapshot_device["sorties"][cle] = self._to_bool(raw)
            except Exception as e:
                self.log(f"Erreur lecture sortie {entity_id} : {e}", level="WARNING")
                snapshot_device["sorties"][cle] = False

        # ── 2. Mesures brutes ────────────────────────────────────────
        # Lecture des valeurs brutes pour calcul des sorties dérivées
        mesures_raw = {}
        for cle, entity_id in device.get("mesures", {}).items():
            if not isinstance(entity_id, str):
                continue
            try:
                mesures_raw[cle] = self.get_state(entity_id)
            except Exception as e:
                self.log(f"Erreur lecture mesure {entity_id} : {e}", level="WARNING")
                mesures_raw[cle] = None

        # ── 3. Sorties dérivées ──────────────────────────────────────
        # Toutes les sorties calculées passent ici — pas de traitement spécial ailleurs
        for cle_sortie in device.get("sorties_derivees", {}).keys():

            if cle_sortie == "pleut":
                snapshot_device["sorties_derivees"]["pleut"] = self._calc_pleut(
                    mesures_raw.get("pluie_mv"), seuils
                )

            elif cle_sortie == "besoin_lumiere":
                snapshot_device["sorties_derivees"]["besoin_lumiere"] = self._calc_besoin_lumiere(
                    device_id, mesures_raw.get("lumiere_mv"), seuils
                )

            elif cle_sortie == "en_lecture":
                snapshot_device["sorties_derivees"]["en_lecture"] = self._calc_etat_media(
                    mesures_raw.get("etat_raw"), seuils.get("valeur_active", "playing")
                )

            elif cle_sortie == "en_pause":
                snapshot_device["sorties_derivees"]["en_pause"] = self._calc_etat_media(
                    mesures_raw.get("etat_raw"), seuils.get("valeur_pause", "paused")
                )

            elif cle_sortie == "immobile":
                snapshot_device["sorties_derivees"]["immobile"] = self._calc_immobile(
                    device_id, device
                )

        # ── 4. Vecteurs — lecture brute ──────────────────────────────
        # Stockage des valeurs brutes LD2450 — pas de traitement ici
        for cle, entity_id in device.get("vecteurs", {}).items():
            if not isinstance(entity_id, str):
                continue
            try:
                snapshot_device["vecteurs"][cle] = self.get_state(entity_id)
            except Exception:
                snapshot_device["vecteurs"][cle] = None

        # ── 5. Vector engine ─────────────────────────────────────────
        # Alimentation du buffer temporel et injection dans sorties_derivees
        # Clés produites : {device_id}_toutes_statiques, _mouvement_local, etc.
        vector_engine = self.vector_engines.get(device_id)
        if vector_engine is not None:
            try:
                vecteurs = snapshot_device["vecteurs"]
                for n in ["1", "2", "3"]:
                    x       = self._to_float(vecteurs.get(f"cible_{n}_x"))
                    y       = self._to_float(vecteurs.get(f"cible_{n}_y"))
                    vitesse = self._to_float(vecteurs.get(f"cible_{n}_vitesse"))
                    if x is not None and y is not None and vitesse is not None:
                        vector_engine.push(f"cible_{n}", x=x, y=y, vitesse=vitesse)

                snapshot_device["sorties_derivees"].update(
                    vector_engine.snapshot()
                )
            except Exception as e:
                self.log(f"vector_engine {device_id} erreur : {e}", level="WARNING")

        self._write_snapshot(device_id, snapshot_device)

    # ─────────────────────────────────────────────
    # Calculs dérivés
    # ─────────────────────────────────────────────

    def _calc_immobile(self, device_id, device):
        """still_target_count > 0 ET moving_target_count == 0"""
        try:
            still_entity  = device.get("vecteurs", {}).get("still_target_count")
            moving_entity = device.get("vecteurs", {}).get("moving_target_count")
            if not still_entity or not moving_entity:
                return False
            still  = int(float(self.get_state(still_entity)  or 0))
            moving = int(float(self.get_state(moving_entity) or 0))
            return still > 0 and moving == 0
        except (TypeError, ValueError):
            return False

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

    def _calc_etat_media(self, raw, valeur_attendue):
        return str(raw) == str(valeur_attendue) if raw is not None else False

    # ─────────────────────────────────────────────
    # Convertisseurs
    # ─────────────────────────────────────────────

    def _to_bool(self, val):
        if val in (True, "on", "ON", "true", "True", "home"):
            return True
        if val in (False, "off", "OFF", "false", "False", "not_home"):
            return False
        return False

    def _to_float(self, val):
        if val is None:
            return None
        try:
            f = float(val)
            return None if f != f else f   # filtre NaN
        except (TypeError, ValueError):
            return None

    # ─────────────────────────────────────────────
    # Écriture dans HA
    # ─────────────────────────────────────────────

    def _write_snapshot(self, device_id, snapshot_device):
        self._vectors_cache[device_id] = {
            "piece":            snapshot_device["piece"],
            "timestamp":        snapshot_device["timestamp"],
            "sorties":          dict(snapshot_device["sorties"]),
            "sorties_derivees": dict(snapshot_device["sorties_derivees"]),
            "vecteurs":         dict(snapshot_device["vecteurs"]),
        }

        try:
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
    # API publique
    # ─────────────────────────────────────────────

    def get_vectors(self):
        return self._vectors_cache

    def get_device(self, device_id):
        return self._vectors_cache.get(device_id)

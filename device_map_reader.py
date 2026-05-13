import appdaemon.plugins.hass.hassapi as hass
import yaml
import json
import os
import time
from datetime import datetime


class DeviceMapReader(hass.Hass):
    """
    Lecteur vectoriel — Complément de signal_reader.py
    ===================================================
    Lit device_map.yaml et produit la section "vectors" du snapshot.json.

    Principe :
    - signal_reader.py  → snapshot["signals"]  (True / False)
    - device_map_reader → snapshot["vectors"]  (float / str / bool enrichis)

    Les deux lecteurs écrivent dans le même snapshot.json sans se marcher dessus.
    moteur_scoring.py consomme les deux sections sans connaître le capteur physique.

    Capteurs gérés :
    - LD2450  (chambre, bureau)  : nb_cibles, vitesse, direction, état dérivé
    - Aqara FP2 (zones vie/nuit) : présence par zone, coordonnées cibles, flux de passage
    - Capteur extérieur          : lux, pluie
    """

    # ─────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────

    def initialize(self):
        self.device_map_path = self.args.get(
            "device_map_path",
            "/config/appdaemon/apps/device_map.yaml"
        )
        self.snapshot_path = self.args.get(
            "snapshot_path",
            "/config/appdaemon/apps/snapshot.json"
        )

        # Registre interne : id_device → {derniere_valeur, timestamp, ...}
        self._state_cache = {}

        # Registre de stabilisation : id_device → timestamp du dernier changement confirmé
        self._stable_since = {}

        # Chargement de la carte
        self.device_map = self._load_device_map()
        if not self.device_map:
            self.log("device_map.yaml vide ou introuvable — abandon", level="ERROR")
            return

        # Abonnements dynamiques à toutes les entités déclarées
        self._subscribe_all()

        self.log("DeviceMapReader initialisé — abonnements actifs")

    # ─────────────────────────────────────────────
    # Chargement
    # ─────────────────────────────────────────────

    def _load_device_map(self):
        if not os.path.exists(self.device_map_path):
            self.log(f"Introuvable : {self.device_map_path}", level="ERROR")
            return {}
        with open(self.device_map_path, "r") as f:
            return yaml.safe_load(f) or {}

    # ─────────────────────────────────────────────
    # Abonnements dynamiques
    # ─────────────────────────────────────────────

    def _subscribe_all(self):
        """
        Parcourt device_map.yaml et s'abonne à toutes les entités triggers.
        Un seul handler générique reçoit tous les changements.
        """
        count = 0
        for piece, contenu in self.device_map.get("pieces", {}).items():
            for section in ("recepteurs", "actionneurs"):
                for device in contenu.get(section, []):
                    device_id = device.get("id")
                    triggers = device.get("triggers", {})
                    seuils = device.get("seuils", {})

                    # On s'abonne à l'entité principale
                    entity_principale = device.get("entity")
                    if entity_principale:
                        self.listen_state(
                            self._on_entity_change,
                            entity_principale,
                            device_id=device_id,
                            piece=piece,
                            device=device,
                            seuils=seuils
                        )
                        count += 1

                    # On s'abonne à chaque trigger secondaire
                    for trigger_key, entity_id in triggers.items():
                        self.listen_state(
                            self._on_entity_change,
                            entity_id,
                            device_id=device_id,
                            piece=piece,
                            device=device,
                            trigger_key=trigger_key,
                            seuils=seuils
                        )
                        count += 1

        self.log(f"Abonnements créés : {count} entités")

    # ─────────────────────────────────────────────
    # Handler principal
    # ─────────────────────────────────────────────

    def _on_entity_change(self, entity, attribute, old, new, kwargs):
        if new == old:
            return

        device_id = kwargs.get("device_id")
        piece = kwargs.get("piece")
        device = kwargs.get("device")
        seuils = kwargs.get("seuils", {})

        # Lecture de l'état complet du device (toutes ses entités)
        vecteur = self._lire_vecteur_device(device, piece, seuils)
        if not vecteur:
            return

        # Application des seuils de stabilisation temporelle
        vecteur = self._appliquer_seuils_temporels(device_id, vecteur, seuils)

        # Dérivation d'un état sémantique selon le type de capteur
        vecteur["etat_derive"] = self._deriver_etat(device_id, vecteur, seuils)

        # Détection de flux de passage (FP2 uniquement)
        if "fp2" in device_id.lower() or "presence" in device_id.lower():
            vecteur["flux"] = self._detecter_flux(device_id, vecteur)

        # Écriture dans le snapshot
        self._write_snapshot(device_id, piece, vecteur)

    # ─────────────────────────────────────────────
    # Lecture vectorielle d'un device
    # ─────────────────────────────────────────────

    def _lire_vecteur_device(self, device, piece, seuils):
        """
        Lit toutes les entités d'un device et retourne un dict normalisé.
        Applique les seuils de valeur (filtre bruit physique).
        """
        vecteur = {
            "piece": piece,
            "timestamp": datetime.now().isoformat(),
        }

        # Entité principale
        entity_principale = device.get("entity")
        if entity_principale:
            val = self.get_state(entity_principale)
            vecteur["presence"] = self._normaliser_bool(val)

        # Triggers secondaires
        triggers = device.get("triggers", {})
        for trigger_key, entity_id in triggers.items():
            try:
                raw = self.get_state(entity_id)
                vecteur[trigger_key] = self._normaliser_valeur(
                    trigger_key, raw, seuils
                )
            except Exception as e:
                self.log(f"Erreur lecture {entity_id} : {e}", level="WARNING")
                vecteur[trigger_key] = None

        return vecteur

    # ─────────────────────────────────────────────
    # Normalisation des valeurs
    # ─────────────────────────────────────────────

    def _normaliser_bool(self, val):
        if val in (True, "on", "ON", "true", "True", "home"):
            return True
        if val in (False, "off", "OFF", "false", "False", "not_home"):
            return False
        return None

    def _normaliser_valeur(self, key, raw, seuils):
        """
        Convertit la valeur brute HA et applique les seuils de valeur.
        - vitesse < seuil_vitesse_silence → ramené à 0 (filtre bruit)
        - direction : conservée telle quelle (str)
        - nb_cibles : int
        - coordonnées x/y : float
        - lux, pluie : float / bool
        """
        if raw is None or raw in ("unavailable", "unknown", ""):
            return None

        # Seuils de valeur appliqués aux vitesses
        if "vitesse" in key:
            try:
                v = float(raw)
                seuil = float(seuils.get("vitesse_silence", 5))
                return 0.0 if v < seuil else round(v, 2)
            except (ValueError, TypeError):
                return None

        # Coordonnées FP2 (x, y, distance)
        if any(k in key for k in ("_x", "_y", "distance")):
            try:
                return round(float(raw), 3)
            except (ValueError, TypeError):
                return None

        # Nombre de cibles
        if "nb_cibles" in key or "nombre" in key:
            try:
                return int(float(raw))
            except (ValueError, TypeError):
                return 0

        # Données météo (lux)
        if "lux" in key or "lumiere" in key:
            try:
                return round(float(raw), 1)
            except (ValueError, TypeError):
                return None

        # Pluie / booléens
        if "pluie" in key or "mouvement" in key or "immobile" in key:
            return self._normaliser_bool(raw)

        # Direction (string)
        if "direction" in key:
            return str(raw) if raw else None

        # Valeur générique
        try:
            return round(float(raw), 3)
        except (ValueError, TypeError):
            return str(raw)

    # ─────────────────────────────────────────────
    # Seuils de stabilisation temporelle
    # ─────────────────────────────────────────────

    def _appliquer_seuils_temporels(self, device_id, vecteur, seuils):
        """
        Compare le nouveau vecteur avec le cache.
        Si la valeur change, enregistre le timestamp du changement.
        Ne propage dans le snapshot que si la valeur est stable
        depuis au moins `stabilisation` secondes.

        Retourne le vecteur à écrire (stable ou précédent si trop tôt).
        """
        delai_stabilisation = float(seuils.get("stabilisation", 0))
        delai_nb_cibles = float(seuils.get("nb_cibles_delai", 0))

        now = time.time()
        cache = self._state_cache.get(device_id, {})
        stable_since = self._stable_since.get(device_id, {})

        vecteur_a_ecrire = dict(vecteur)

        for key, new_val in vecteur.items():
            if key in ("timestamp", "piece"):
                continue

            old_val = cache.get(key)

            if new_val != old_val:
                # Nouveau changement détecté → on enregistre le moment
                if key not in stable_since or old_val != cache.get(f"_pending_{key}"):
                    stable_since[key] = now
                    cache[f"_pending_{key}"] = new_val

                # Délai selon le type de clé
                delai = delai_nb_cibles if "nb_cibles" in key else delai_stabilisation

                if delai > 0 and (now - stable_since.get(key, 0)) < delai:
                    # Pas encore stable — on garde l'ancienne valeur dans le snapshot
                    vecteur_a_ecrire[key] = old_val
                else:
                    # Stable — on valide le changement
                    cache[key] = new_val
                    stable_since.pop(key, None)
            else:
                # Valeur identique — on stabilise
                stable_since.pop(key, None)

        self._state_cache[device_id] = cache
        self._stable_since[device_id] = stable_since

        return vecteur_a_ecrire

    # ─────────────────────────────────────────────
    # Dérivation d'état sémantique
    # ─────────────────────────────────────────────

    def _deriver_etat(self, device_id, vecteur, seuils):
        """
        Produit un état lisible par moteur_scoring depuis les valeurs brutes.

        LD2450 (chambre / bureau) :
          absent | dort | lit | actif

        FP2 (zones) :
          absent | present_immobile | present_en_mouvement | transit

        Capteur extérieur :
          jour_ensoleille | jour_nuageux | pluie | nuit
        """

        # ── Capteur extérieur ──────────────────────
        if "meteo" in device_id.lower() or "ext" in device_id.lower():
            pluie = vecteur.get("pluie")
            lux = vecteur.get("lux") or vecteur.get("lumiere")
            if pluie:
                return "pluie"
            if lux is not None:
                if lux < 10:
                    return "nuit"
                if lux < 200:
                    return "jour_nuageux"
                return "jour_ensoleille"
            return "inconnu"

        # ── LD2450 ─────────────────────────────────
        if "ld2450" in device_id.lower() or "capteur_mvt" in device_id.lower():
            presence = vecteur.get("presence")
            if not presence:
                return "absent"

            nb = vecteur.get("nb_cibles") or 0
            # Vitesse max parmi les cibles suivies
            vitesses = [
                vecteur.get(f"cible_{i}_vitesse") or 0
                for i in range(1, 4)
            ]
            vitesse_max = max(vitesses)
            seuil_silence = float(seuils.get("vitesse_silence", 5))

            if nb == 0:
                return "absent"
            if vitesse_max <= seuil_silence:
                # Immobile — différenciation dort / lit
                # "dort" si stable depuis > stabilisation
                stable_sec = self._temps_stable(device_id, "nb_cibles")
                seuil_stable = float(seuils.get("stabilisation", 120))
                if stable_sec > seuil_stable:
                    return "dort"
                return "lit"
            return "actif"

        # ── Aqara FP2 ──────────────────────────────
        presence = vecteur.get("presence")
        if not presence:
            return "absent"

        # Vitesse de la cible principale
        vitesse = vecteur.get("cible_1_vitesse") or 0
        seuil_silence = float(seuils.get("vitesse_silence", 5))

        # Détection transit : présence + déplacement rapide vers une porte
        if vecteur.get("flux", {}).get("en_transit"):
            return "transit"

        if vitesse <= seuil_silence:
            return "present_immobile"
        return "present_en_mouvement"

    # ─────────────────────────────────────────────
    # Détection flux de passage (FP2)
    # ─────────────────────────────────────────────

    def _detecter_flux(self, device_id, vecteur):
        """
        Analyse les coordonnées X/Y des cibles FP2 pour détecter :
        - une approche vers une porte (zone de passage définie dans device_map)
        - une direction de transit (entrant / sortant)

        Retourne un dict : {
            "en_transit": bool,
            "direction": "entrant" | "sortant" | None,
            "zone_cible": str | None
        }
        """
        flux = {
            "en_transit": False,
            "direction": None,
            "zone_cible": None
        }

        x1 = vecteur.get("cible_1_x")
        y1 = vecteur.get("cible_1_y")
        vitesse = vecteur.get("cible_1_vitesse") or 0

        if x1 is None or y1 is None or vitesse == 0:
            return flux

        # Récupération des zones portes depuis device_map
        zones_portes = self._get_zones_portes(device_id)

        for zone_id, zone in zones_portes.items():
            x_min = zone.get("x_min", 0)
            x_max = zone.get("x_max", 999)
            y_min = zone.get("y_min", 0)
            y_max = zone.get("y_max", 999)

            if x_min <= x1 <= x_max and y_min <= y1 <= y_max:
                flux["en_transit"] = True
                flux["zone_cible"] = zone_id
                # Direction estimée depuis la vitesse signée (si disponible)
                direction_raw = vecteur.get("cible_1_direction")
                if direction_raw:
                    flux["direction"] = str(direction_raw)
                break

        return flux

    def _get_zones_portes(self, device_id):
        """
        Extrait les zones de portes définies dans device_map.yaml
        pour le device FP2 concerné.
        Format attendu dans device_map.yaml :
          zones_portes:
            porte_salon:
              x_min: 1.0
              x_max: 1.5
              y_min: 0.0
              y_max: 4.0
        """
        for piece, contenu in self.device_map.get("pieces", {}).items():
            for device in contenu.get("recepteurs", []):
                if device.get("id") == device_id:
                    return device.get("zones_portes", {})
        return {}

    # ─────────────────────────────────────────────
    # Utilitaires
    # ─────────────────────────────────────────────

    def _temps_stable(self, device_id, key):
        """Retourne le nombre de secondes depuis le dernier changement confirmé."""
        cache = self._state_cache.get(device_id, {})
        stable_since = self._stable_since.get(device_id, {})
        if key not in stable_since:
            # Pas de changement en attente = valeur stable depuis longtemps
            return 9999
        return time.time() - stable_since[key]

    # ─────────────────────────────────────────────
    # Écriture snapshot
    # ─────────────────────────────────────────────

    def _write_snapshot(self, device_id, piece, vecteur):
        """
        Lit le snapshot existant, met à jour la section "vectors",
        et réécrit le fichier atomiquement.
        signal_reader.py écrit dans "signals" — on ne touche pas à cette section.
        """
        snapshot = {}

        try:
            if os.path.exists(self.snapshot_path):
                with open(self.snapshot_path, "r") as f:
                    snapshot = json.load(f)
        except Exception as e:
            self.log(f"Erreur lecture snapshot : {e}", level="WARNING")

        if "vectors" not in snapshot:
            snapshot["vectors"] = {}

        snapshot["vectors"][device_id] = {
            "piece": piece,
            "timestamp": vecteur.get("timestamp"),
            "etat_derive": vecteur.get("etat_derive"),
            "valeurs": {
                k: v for k, v in vecteur.items()
                if k not in ("piece", "timestamp", "etat_derive")
            }
        }

        try:
            tmp_path = self.snapshot_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.snapshot_path)

            self.log(
                f"Snapshot mis à jour — {device_id} → {vecteur.get('etat_derive')}",
                level="DEBUG"
            )
        except Exception as e:
            self.log(f"Erreur écriture snapshot : {e}", level="ERROR")

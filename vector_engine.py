# ─────────────────────────────────────────────────────────────────
# vector_engine.py
# Moteur Vectoriel Temporel — Projet JARVIS / GEMMA
#
# Rôle : calculer des indicateurs temporels à partir des
#        coordonnées cartésiennes x/y exposées par ESPHome
#        sur une fenêtre glissante temporelle.
#
# Principe de responsabilité unique (SRP) :
#   - Aucune dépendance à Home Assistant
#   - Aucune dépendance à AppDaemon
#   - Aucune lecture YAML
#   - Entrée  : push_point(x, y, vitesse)
#   - Sortie  : indicateurs physiques consommables par GEMMA
#
# Ce module NE recalcule PAS :
#   - Les zones Z2/Z3 → déjà calculées par l'ESP32 (sur_le_lit, passage_porte)
#   - La présence globale Z1 → déjà exposée par LD2450 natif
#   - immobile natif → still_target_count / moving_target_count LD2450
#
# Ce module calcule UNIQUEMENT :
#   - variance_position  : dispersion x/y normalisée sur fenêtre glissante
#                          0.0 → 1.0 (normalisé par bornes Z1)
#                          ≈ 0.0  → statique  (sommeil, lecture)
#                          faible → local     (habillage, travail)
#                          élevé  → ample     (ménage, entretien)
#
#   - kinetic_integral   : intégrale vitesse × temps (énergie cinétique)
#                          ≈ 0    → stationnaire
#                          élevé  → déplacement réel cumulé
#
#   - is_static          : variance < SEUIL_STATIQUE
#   - is_local           : SEUIL_STATIQUE ≤ variance < SEUIL_AMPLE
#   - is_moving          : variance ≥ SEUIL_AMPLE
#
# Bornes Z1 — pièce entière (chambre) :
#   X : -1850 à +1850 mm
#   Y :     0 à  3400 mm
#   → NORM = 1850² + 3400² ≈ 15 millions mm²
#
# Seuils — à calibrer via observation_mode=True sur le terrain :
#   SEUIL_STATIQUE  : variance normalisée en dessous = immobile
#   SEUIL_AMPLE     : variance normalisée au dessus  = grands déplacements
#   WINDOW_SECONDS  : durée fenêtre glissante
#
# ─────────────────────────────────────────────────────────────────

import time
import math
import logging
from collections import deque

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# BORNES Z1 — normalisent la variance entre 0 et 1
# Adapter si la pièce change (bureau différent de chambre)
# ─────────────────────────────────────────────
X_MAX = 1850.0   # mm — demi-largeur pièce
Y_MAX = 3400.0   # mm — profondeur totale pièce
NORM  = X_MAX ** 2 + Y_MAX ** 2   # ≈ 15 002 500 mm²

# ─────────────────────────────────────────────
# SEUILS — à calibrer via mode observation
# Valeurs initiales indicatives, non validées terrain
# ─────────────────────────────────────────────
SEUIL_STATIQUE   = 0.001   # variance normalisée < seuil → immobile
SEUIL_AMPLE      = 0.050   # variance normalisée > seuil → grands déplacements
WINDOW_SECONDS   = 10.0    # secondes — fenêtre glissante
SAMPLING_RATE_HZ = 2       # Hz — fréquence LD2450 estimée


# ─────────────────────────────────────────────
# CLASSE : buffer circulaire par cible
# ─────────────────────────────────────────────
class TargetTrack:
    """
    Buffer circulaire temporel pour une cible LD2450.

    Entrée : coordonnées cartésiennes x/y en mm
             exposées nativement par ESPHome (c1x, c1y, etc.)
    Sortie : variance_position normalisée + kinetic_integral

    Usage depuis device_map_reader :
        track = TargetTrack()
        track.push_point(x=c1x, y=c1y, vitesse=speed)
        print(track.is_static)
        print(track.variance_position)
    """

    def __init__(self,
                 window_seconds: float = WINDOW_SECONDS,
                 sampling_rate: int = SAMPLING_RATE_HZ):
        max_points       = int(window_seconds * sampling_rate)
        self.buffer_x    = deque(maxlen=max_points)
        self.buffer_y    = deque(maxlen=max_points)
        self.buffer_v    = deque(maxlen=max_points)
        self.buffer_time = deque(maxlen=max_points)

    # ── Alimentation ──────────────────────────

    def push_point(self, x: float, y: float, vitesse: float):
        """
        Pousser un point de mesure cartésien.

        x       : coordonnée X en mm  (c1x depuis HA)
        y       : coordonnée Y en mm  (c1y depuis HA)
        vitesse : vitesse en m/s      (speed depuis HA)

        Points ignorés :
          - valeurs None ou NaN
          - point (0, 0) = cible absente dans le firmware LD2450
        """
        if x is None or y is None or vitesse is None: return
        try:
            x, y, vitesse = float(x), float(y), float(vitesse)
        except (ValueError, TypeError):
            return
        if math.isnan(x) or math.isnan(y) or math.isnan(vitesse): return
        if x == 0.0 and y == 0.0: return   # cible absente

        self.buffer_x.append(x)
        self.buffer_y.append(y)
        self.buffer_v.append(abs(vitesse))
        self.buffer_time.append(time.time())

    # ── Nettoyage fenêtre ─────────────────────

    def _purge(self):
        """Supprimer les points hors fenêtre temporelle."""
        cutoff = time.time() - WINDOW_SECONDS
        while self.buffer_time and self.buffer_time[0] < cutoff:
            self.buffer_time.popleft()
            self.buffer_x.popleft()
            self.buffer_y.popleft()
            self.buffer_v.popleft()

    @property
    def nb_points(self) -> int:
        self._purge()
        return len(self.buffer_time)

    # ── Indicateurs ──────────────────────────

    @property
    def variance_position(self) -> float:
        """
        Dispersion spatiale x/y sur la fenêtre, normalisée entre 0.0 et 1.0.

        Calcul :
          var_x = variance des coordonnées X
          var_y = variance des coordonnées Y
          variance_position = (var_x + var_y) / NORM

        Interprétation (seuils indicatifs, à calibrer) :
          < SEUIL_STATIQUE  → immobile   (sommeil, lecture concentrée)
          < SEUIL_AMPLE     → local      (habillage, travail bureau)
          ≥ SEUIL_AMPLE     → ample      (ménage, entretien)
        """
        self._purge()
        n = len(self.buffer_x)
        if n < 2:
            return 0.0

        xs = list(self.buffer_x)
        ys = list(self.buffer_y)

        mean_x = sum(xs) / n
        mean_y = sum(ys) / n

        var_x = sum((v - mean_x) ** 2 for v in xs) / n
        var_y = sum((v - mean_y) ** 2 for v in ys) / n

        return (var_x + var_y) / NORM

    @property
    def kinetic_integral(self) -> float:
        """
        Intégrale vitesse × temps sur la fenêtre (m).
        Représente le déplacement cumulé réel de la cible.

        ≈ 0.0  → stationnaire
        élevé  → déplacement important (traversée de pièce)
        """
        self._purge()
        n = len(self.buffer_v)
        if n < 2:
            return 0.0

        vitesses = list(self.buffer_v)
        temps    = list(self.buffer_time)

        integral = 0.0
        for i in range(1, n):
            dt          = temps[i] - temps[i - 1]
            moy_vitesse = (vitesses[i] + vitesses[i - 1]) / 2.0
            integral   += moy_vitesse * dt

        return round(integral, 4)

    @property
    def is_static(self) -> bool:
        """Cible immobile — sommeil, lecture, travail concentré."""
        return self.variance_position < SEUIL_STATIQUE

    @property
    def is_local(self) -> bool:
        """Mouvements locaux — habillage, vaisselle."""
        return SEUIL_STATIQUE <= self.variance_position < SEUIL_AMPLE

    @property
    def is_moving(self) -> bool:
        """Grands déplacements — ménage, entretien."""
        return self.variance_position >= SEUIL_AMPLE

    # ── Observation ──────────────────────────

    def observation_log(self, label: str):
        """
        Logguer les valeurs brutes pour calibration terrain.
        Activer RoomVectorEngine(observation_mode=True) pendant
        une session réelle pour mesurer les distributions.
        """
        logger.info(
            "[OBS] %s | pts=%d | var=%.6f | kinetic=%.4f | "
            "static=%s local=%s moving=%s",
            label,
            self.nb_points,
            self.variance_position,
            self.kinetic_integral,
            self.is_static,
            self.is_local,
            self.is_moving,
        )


# ─────────────────────────────────────────────
# CLASSE : gestionnaire multi-cibles par pièce
# ─────────────────────────────────────────────
class RoomVectorEngine:
    """
    Gestionnaire vectoriel pour une pièce — jusqu'à 3 cibles (limite LD2450).

    Usage depuis device_map_reader (AppDaemon) :

        # Initialisation (une fois dans initialize())
        self.vector_chambre = RoomVectorEngine(
            piece_id="Presence_chambre",
            observation_mode=True    # True pendant calibration
        )

        # À chaque callback de mise à jour des capteurs
        self.vector_chambre.push(
            cible_id = "cible_1",
            x        = float(self.get_state("sensor.capteur_mvt_chambre_cible_1_x")),
            y        = float(self.get_state("sensor.capteur_mvt_chambre_cible_1_y")),
            vitesse  = float(self.get_state("sensor.capteur_mvt_chambre_cible_1_vitesse")),
        )

        # Dans le snapshot GEMMA
        snapshot.update(self.vector_chambre.snapshot())
    """

    MAX_CIBLES = 3

    def __init__(self, piece_id: str, observation_mode: bool = False):
        self.piece_id         = piece_id
        self.observation_mode = observation_mode
        self.cibles: dict[str, TargetTrack] = {}

    def push(self, cible_id: str, x: float, y: float, vitesse: float):
        """
        Pousser un point pour une cible identifiée.
        Appelé par device_map_reader à chaque mise à jour HA.
        """
        if cible_id not in self.cibles:
            self.cibles[cible_id] = TargetTrack()

        self.cibles[cible_id].push_point(x, y, vitesse)

        if self.observation_mode:
            self.cibles[cible_id].observation_log(
                f"{self.piece_id}/{cible_id}"
            )

    def _actives(self) -> list[TargetTrack]:
        """Cibles avec suffisamment de points dans la fenêtre."""
        return [t for t in self.cibles.values() if t.nb_points >= 2]

    # ── Agrégats multi-cibles ─────────────────

    @property
    def toutes_statiques(self) -> bool:
        """
        True si toutes les cibles actives sont immobiles.
        Cas typique : couple qui dort, deux personnes qui lisent.
        """
        actives = self._actives()
        if not actives:
            return False
        return all(t.is_static for t in actives)

    @property
    def mouvement_local(self) -> bool:
        """
        True si mouvements locaux sans grands déplacements.
        Cas typique : habillage, vaisselle, travail bureau.
        """
        actives = self._actives()
        if not actives:
            return False
        return (any(t.is_local  for t in actives) and
                not any(t.is_moving for t in actives))

    @property
    def grands_deplacements(self) -> bool:
        """
        True si au moins une cible parcourt la pièce.
        Cas typique : ménage, entretien.
        """
        return any(t.is_moving for t in self._actives())

    @property
    def nb_cibles_actives(self) -> int:
        return len(self._actives())

    # ── Snapshot GEMMA ────────────────────────

    def snapshot(self) -> dict:
        """
        Produit un dict de signaux consommables par GEMMA.
        Clés nommées {piece_id}_{signal} — cohérent avec poids.yaml.

        Signaux produits :
          {piece_id}_toutes_statiques     : bool
          {piece_id}_mouvement_local      : bool
          {piece_id}_grands_deplacements  : bool
          {piece_id}_nb_cibles_actives    : int
          {piece_id}_cible_N_variance     : float  (debug/calibration)
          {piece_id}_cible_N_kinetic      : float  (debug/calibration)
        """
        actives = self._actives()
        base = {
            f"{self.piece_id}_toutes_statiques":    self.toutes_statiques,
            f"{self.piece_id}_mouvement_local":     self.mouvement_local,
            f"{self.piece_id}_grands_deplacements": self.grands_deplacements,
            f"{self.piece_id}_nb_cibles_actives":   self.nb_cibles_actives,
        }
        # Détail par cible — pour debug et calibration terrain
        for i, track in enumerate(actives):
            base[f"{self.piece_id}_cible_{i+1}_variance"] = round(track.variance_position, 6)
            base[f"{self.piece_id}_cible_{i+1}_kinetic"]  = track.kinetic_integral
        return base

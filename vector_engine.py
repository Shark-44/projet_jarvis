# ─────────────────────────────────────────────────────────────────
# vector_engine.py
# Moteur Vectoriel Temporel — Projet JARVIS / GEMMA
# ─────────────────────────────────────────────────────────────────

import time
import math
import logging
from collections import deque

logger = logging.getLogger(__name__)

X_MAX = 1850.0   # mm — demi-largeur pièce
Y_MAX = 3400.0   # mm — profondeur totale pièce
NORM  = X_MAX ** 2 + Y_MAX ** 2   # ≈ 15 002 500 mm²

SEUIL_STATIQUE   = 0.001   # variance normalisée < seuil → immobile
SEUIL_AMPLE      = 0.050   # variance normalisée > seuil → grands déplacements
WINDOW_SECONDS   = 10.0    # secondes — fenêtre glissante
SAMPLING_RATE_HZ = 2       # Hz — fréquence LD2450 estimée

# Temps de rémanence (en secondes) pour considérer qu'un sommeil est "récent"
DUREE_REMANENCE_SOMMEIL = 900.0  # 15 minutes


class TargetTrack:
    """Buffer circulaire temporel pour une cible LD2450."""

    def __init__(self, window_seconds: float = WINDOW_SECONDS, sampling_rate: int = SAMPLING_RATE_HZ):
        max_points = int(window_seconds * sampling_rate)
        self.buffer_x = deque(maxlen=max_points)
        self.buffer_y = deque(maxlen=max_points)
        self.buffer_v = deque(maxlen=max_points)
        self.buffer_time = deque(maxlen=max_points)

    def push_point(self, x: float, y: float, vitesse: float):
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

    def _purge(self):
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

    @property
    def variance_position(self) -> float:
        self._purge()
        n = len(self.buffer_x)
        if n < 2: return 0.0

        xs, ys = list(self.buffer_x), list(self.buffer_y)
        mean_x, mean_y = sum(xs) / n, sum(ys) / n
        var_x = sum((v - mean_x) ** 2 for v in xs) / n
        var_y = sum((v - mean_y) ** 2 for v in ys) / n

        return (var_x + var_y) / NORM

    @property
    def kinetic_integral(self) -> float:
        self._purge()
        n = len(self.buffer_v)
        if n < 2: return 0.0

        vitesses, temps = list(self.buffer_v), list(self.buffer_time)
        integral = 0.0
        for i in range(1, n):
            dt = temps[i] - temps[i - 1]
            integral += ((vitesses[i] + vitesses[i - 1]) / 2.0) * dt
        return round(integral, 4)

    @property
    def is_static(self) -> bool:
        return self.variance_position < SEUIL_STATIQUE

    @property
    def is_local(self) -> bool:
        return SEUIL_STATIQUE <= self.variance_position < SEUIL_AMPLE

    @property
    def is_moving(self) -> bool:
        return self.variance_position >= SEUIL_AMPLE


class RoomVectorEngine:
    """Gestionnaire vectoriel pour une pièce — avec mémoire d'état court terme."""

    def __init__(self, piece_id: str, observation_mode: bool = False):
        self.piece_id = piece_id
        self.observation_mode = observation_mode
        self.cibles: dict[str, TargetTrack] = {}
        
        # ── AJOUT : Propriétés de mémoire d'état ───────────────────
        self.last_sommeil_timestamp = None

    def push(self, cible_id: str, x: float, y: float, vitesse: float):
        if cible_id not in self.cibles:
            self.cibles[cible_id] = TargetTrack()
        self.cibles[cible_id].push_point(x, y, vitesse)
        
        # À chaque mise à jour, si un sommeil global est détecté, on rafraîchit le timestamp
        if self.toutes_statiques and self.nb_cibles_actives > 0:
            # Note : On pourrait croiser ici avec la zone lit (Z2) si passée en paramètre
            self.last_sommeil_timestamp = time.time()

    def _actives(self) -> list[TargetTrack]:
        return [t for t in self.cibles.values() if t.nb_points >= 2]

    @property
    def toutes_statiques(self) -> bool:
        actives = self._actives()
        if not actives: return False
        return all(t.is_static for t in actives)

    @property
    def mouvement_local(self) -> bool:
        actives = self._actives()
        if not actives: return False
        return any(t.is_local for t in actives) and not any(t.is_moving for t in actives)

    @property
    def grands_deplacements(self) -> bool:
        return any(t.is_moving for t in self._actives())

    @property
    def nb_cibles_actives(self) -> int:
        return len(self._actives())

    # ── AJOUT : Propriété calculée pour le réveil physiologique ────
    @property
    def sommeil_recent(self) -> bool:
        """Renvoie True si un sommeil a été détecté il y a moins de X minutes."""
        if self.last_sommeil_timestamp is None:
            return False
        return (time.time() - self.last_sommeil_timestamp) < DUREE_REMANENCE_SOMMEIL

    def snapshot(self) -> dict:
        """Produit le dictionnaire de signaux consolidés pour GEMMA."""
        actives = self._actives()
        base = {
            f"{self.piece_id}_toutes_statiques":     self.toutes_statiques,
            f"{self.piece_id}_mouvement_local":      self.mouvement_local,
            f"{self.piece_id}_grands_deplacements":  self.grands_deplacements,
            f"{self.piece_id}_nb_cibles_actives":    self.nb_cibles_actives,
            # Nouveau flag indispensable pour GEMMA :
            f"{self.piece_id}_sommeil_recent":       self.sommeil_recent,
        }
        for i, track in enumerate(actives):
            base[f"{self.piece_id}_cible_{i+1}_variance"] = round(track.variance_position, 6)
            base[f"{self.piece_id}_cible_{i+1}_kinetic"]  = track.kinetic_integral
        return base
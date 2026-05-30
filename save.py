# ── Résolution conflits de pièce ─────────
        etats_config   = self.comportements.get("etats", {})
        actions_finales = {}   # { actionneur_id: valeur }
        scores_finaux   = {}   # { actionneur_id: score } pour arbitrage

        # Table de correspondance logicielle (Option 2 - Fallback intelligent)
        # Permet de mapper un état transitoire ou inconnu sur un état existant du YAML
        REMAPPAGE_ETATS = {
            "ENTREE_CHAMBRE": "HABILLAGE", # Si on entre, on prend par défaut les lumières d'habillage
            "TRANSIT_SALON": "REPOS_TV"    # Exemple pour une autre pièce
        }

        for personne, data in etats_personnes.items():
            etat  = data.get("etat")
            score = data.get("score", 0.0)

            self.log(f" {personne:10s} : etat={etat!r}  score={score}")

            if not etat or etat == "INDETERMINE":
                self.log(f"  Etat {etat!r} = ignore")
                continue

            # --- LOGIQUE DE FALLBACK / CORRESPONDANCE ---
            etat_cible = etat
            if etat not in etats_config and etat in REMAPPAGE_ETATS:
                etat_cible = REMAPPAGE_ETATS[etat]
                self.log(f"  [FALLBACK] État {etat!r} non configuré. Redirection vers {etat_cible!r}")

            config_etat = etats_config.get(etat_cible)
            if not config_etat:
                self.log(f"  Aucun comportement trouvé pour {etat_cible!r}")
                continue
            # --------------------------------------------

            actions = config_etat.get(meteo)
            if not actions:
                self.log(f"  Aucune action pour {etat_cible!r} × {meteo!r}")
                continue

            self.log(f" {etat_cible} × {meteo} → {len(actions)} actionneur(s)")

            for actionneur_id, valeur in actions.items():
                score_existant = scores_finaux.get(actionneur_id, -1)
                if score > score_existant:
                    # Si l'état provient d'un remappage (ex: ENTREE_CHAMBRE), 
                    # on peut éventuellement altérer la valeur ici (ex: diviser la luminosité par 2)
                    if etat == "ENTREE_CHAMBRE" and isinstance(valeur, (int, float)) and valeur > 40:
                        valeur = int(valeur * 0.5) # Ambiance accueil plus douce
                        
                    actions_finales[actionneur_id] = valeur
                    scores_finaux[actionneur_id]   = score



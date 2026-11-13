# Progress log

## État actuel — 2026-11-13

**Phase en cours :** Phase 1 — étape 1.2 (introspection) codée, en attente de commit. Étape 1.1 commitée (`b5ac788`).

### Fait
- Phase 0 : squelette du dépôt, config typée, CI (commit `5be3fc3`).
- Étape 1.1 :
  - `src/text2sql/db/schema.sql` : 7 tables (categories, products, stores, customers, orders, order_items, inventory), PK/FK, CHECK, index sur FK, `COMMENT ON` destinés au LLM.
  - `src/text2sql/db/seed.py` : génération déterministe (seed 42) en Python pur + chargement par `COPY` + `setval` des séquences + `ANALYZE`.
  - Volume complet : 5 000 clients, 500 produits, 30 magasins (27 physiques + 3 en ligne), 40 000 commandes, ~94 000 lignes, 13 500 lignes de stock. Période 2024-01-01 → 2025-12-31.
  - Tests unitaires du générateur (déterminisme, intégrité FK, règles métier) + test d'intégration (skippé sans Postgres).
  - CI : nouveau job `integration` avec service PostgreSQL 16 + smoke test du seed complet.

- Étape 1.2 :
  - `src/text2sql/schema/models.py` : modèles Pydantic `DatabaseSchema` / `TableInfo` / `ColumnInfo` / `ForeignKeyInfo`, avec `neighbors()` (voisins par FK, dans les deux sens) et `subset()` — préparent le filtrage de l'étape 1.3.
  - `src/text2sql/schema/introspect.py` : `introspect(engine)` via `sqlalchemy.inspect` : tables, colonnes, types, nullabilité, PK (y compris composite), FK, commentaires, estimation du nombre de lignes (`pg_class.reltuples`), valeurs d'exemple des colonnes texte à ≤ 20 valeurs distinctes. ~0,7 s sur la base complète.
  - Tests d'intégration sur une base dédiée `retail_test` (créée automatiquement) → les tests n'écrasent plus la base `retail`.

### Décisions
- **Modèle : `claude-haiku-5-5` partout (dev ET éval)** — le moins cher (0,10 $ / 0,50 $ par MTok si prompt ≤ 100K tokens). Configuré dans `.env`, jamais en dur. Effort `low` par défaut.
- Contraintes Haiku 5.5 à respecter en Phase 2 (client LLM) :
  - **Pas de `temperature` / `top_p` / `top_k`** (400) → vérifier qu'`instructor` n'en envoie pas ; le déterminisme vient du cache + structured outputs.
  - **Pas de prefill** assistant (400) → le format JSON passe par structured outputs / tool.
  - **Thinking adaptatif actif par défaut** → lire les blocs par `type`, jamais `content[0]` ; les tokens de thinking comptent dans `max_tokens` (4096 de marge).
  - Ne pas envoyer `budget_tokens` (400). Levier de coût = `output_config.effort`.
  - `tool_choice` forcé accepté mais saute le thinking → acceptable pour instructor (mode tools) ; on comparera avec le mode JSON.
  - Pas de fallback serveur sur refus → gérer `stop_reason == "refusal"` explicitement.
  - Garder les prompts < 100K tokens (sinon tarif ×5) — largement le cas ici.
- Limite assumée : l'éval avec Haiku donnera des chiffres plus bas qu'avec un gros modèle ; à documenter dans le README (« Limitations »).
- Dépendances ajoutées phase par phase (pas de torch tant qu'on ne s'en sert pas).
- Données : génération séparée du chargement → testable sans base. IDs explicites générés en Python (FK cohérentes), puis `setval`.
- Pièges d'ambiguïté volontaires dans le schéma : CA brut vs net (`discount`), statuts (`cancelled`/`returned`/`pending`), `order_date` vs `shipped_date`, prix catalogue (`products.unit_price`) vs prix de vente (`order_items.unit_price`, ~4 % plus bas en 2024).
- Cohérence interne (utile pour les sanity checks de la Phase 3) : pas de commande avant l'inscription du client ni avant l'ouverture du magasin, produit vendu seulement après sa création, `loyalty_tier` dérivé des dépenses réelles.
- `.gitattributes` force LF (scripts exécutés dans des conteneurs Linux).

- Introspection : catégoriel = colonne texte avec ≤ 20 valeurs distinctes (seuil configurable). Requêtes construites avec SQLAlchemy Core (identifiants quotés), jamais par f-string. Estimation de lignes via `pg_class` plutôt que `COUNT(*)` (instantané, exact après `ANALYZE`).
- Effet de bord connu : `categories.description` est détecté comme catégoriel (12 valeurs) — inoffensif, à revoir si le prompt devient trop long.

### Reste à faire
- Phase 1 : 1.3 embedder + filtrage, 1.4 prompt builder, 1.5 ambiguïté.
- Phases 2 à 6.

### Prochaine étape
- Committer l'étape 1.2, puis étape 1.3 (embedder local + filtrage du schéma par similarité + expansion FK).

### Blocages
- ~~Docker absent~~ → résolu (2026-11-13) : Docker Desktop 29.9 + WSL 2 installés, PostgreSQL 16 lancé via compose, seed complet chargé en ~24 s, test d'intégration vert. Pour reprendre : lancer Docker Desktop puis `docker compose up -d db` (les données persistent dans le volume `pgdata`).
- Pas encore de remote GitHub configuré.

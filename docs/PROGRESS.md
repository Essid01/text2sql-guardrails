# Progress log

## État actuel — 2026-11-13

**Phase en cours :** Phase 2 — étape 2.1 (client LLM + cache) codée, en attente de commit. Phase 1 commitée (`e20db0d`, `25e4ce4`).

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
- Étape 1.3 :
  - `schema/embedder.py` : interface `Embedder` (Protocol) ; `SentenceTransformerEmbedder` (local, CPU, import paresseux) ; `HashingEmbedder` déterministe pour les tests/CI.
  - `schema/retriever.py` : `SchemaRetriever` = top-k par similarité cosinus + ajout des tables « pont » (plus court chemin FK entre chaque paire retenue, BFS). Résultat explicable (`scores`, `by_similarity`, `added_as_bridge`).
  - `sentence-transformers` en extra optionnel `[embeddings]` (torch ~1 Go) → CI sans torch.
  - Commentaires enrichis sur `orders` / `order_items` (dictionnaire de données : « source of all revenue… »).

- Étape 1.4 :
  - `prompt/schema_render.py` : schéma rendu en DDL commenté (types, PK/FK, commentaires, valeurs catégorielles, nb de lignes). Schéma complet ≈ 1 200 tokens.
  - `prompt/templates/sql_system.md` : system prompt : rôle, ancrage au schéma, conventions métier, style SQL, cas de refus/clarification.
  - `prompt/examples.yaml` : 6 exemples few-shot écrits à la main, chacun exécuté par un test d'intégration.
  - `prompt/builder.py` : `PromptBuilder` → system statique + message user `<schema>` / `<examples>` / `<question>` ; 3 exemples choisis par similarité (embedder) ou dans l'ordre (sans embedder) ; schéma complet ou filtré (retriever optionnel). Prompt complet ≈ 2 300 tokens.
- Étape 1.5 :
  - `prompt/glossary.yaml` + `prompt/ambiguity.py` : détection déterministe des ambiguïtés connues **avant** l'appel LLM (CA brut/net, mois sans année, « meilleurs clients » sans métrique), en EN et FR ; `apply_clarification()` réécrit la question avec l'option choisie.
  - Invariant testé : chaque option résout l'ambiguïté qu'elle clarifie (la boucle de clarification se termine toujours).
- Démo : `python -m text2sql.prompt.show "question"` affiche les clarifications éventuelles et le prompt complet, sans appel LLM.
- Tests : 73 (unitaires + intégration + vrai embedder), tous verts.
- Étape 2.1 :
  - `llm/client.py` : interface `LLMClient` ; `AnthropicClient` (SDK natif `messages.parse`, effort dans `output_config`, aucun paramètre d'échantillonnage, refus et troncature → exceptions typées, 1 nouvelle tentative si un validateur Pydantic échoue) ; `FakeLLMClient` ; `UsageTracker` (appels, tokens, coût).
  - `llm/cache.py` : `CachedLLMClient` (SQLite, clé = hash modèle + paramètres + prompts + schéma de sortie). Un hit coûte 0 et garde les tokens d'origine.
  - `llm/factory.py` : client configuré depuis `.env`.
  - Tests : 10 unitaires (SDK simulé) + 1 test réel `-m llm`.

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

- **Filtrage du schéma : désactivé par défaut (`SCHEMA_TOP_K` non défini = schéma complet).** Mesure sur 8 questions écrites à la main (EN + FR), base complète :

  | Modèle | k | Rappel EN | Rappel FR | Tables gardées (/7) |
  |---|---|---|---|---|
  | all-MiniLM-L6-v2 | 3 | 5/8 | 6/8 | 3,9–4,8 |
  | all-MiniLM-L6-v2 | 4 | 7/8 | 8/8 | 5,2–5,6 |
  | paraphrase-multilingual-MiniLM-L12-v2 | 4 | 7/8 | 5/8 | 4,6–5,1 |

  (rappel = toutes les tables nécessaires sont retenues ; mesure après enrichissement des commentaires pour MiniLM-L6.) Avec 7 tables, filtrer économise peu et fait parfois perdre une table indispensable (ex. `orders` pour un filtre d'année : aucun mot de la question ne la désigne). Le retriever reste dans le code et sera comparé au schéma complet en Phase 5 (50 questions). Modèle retenu : `all-MiniLM-L6-v2` (le multilingue n'est pas meilleur, même en français, et 2× plus lent).

- System prompt : règles accompagnées de leur raison (plutôt que des MAJUSCULES) ; le prompt n'est PAS la sécurité (guardrails en Phase 2 = défense en profondeur).
- Ambiguïtés : glossaire déterministe (gratuit, testable) + le LLM pourra demander une clarification pour les cas inconnus (Phase 2). Mois détecté seulement après une préposition (« in May », « en mars ») pour éviter « may » (verbe) et « Mars » (marque).
- Les questions de `examples.yaml` ne doivent **pas** être réutilisées dans le jeu d'évaluation (Phase 5).

- **SDK natif au lieu d'instructor** (choisi sur mesure réelle avec Haiku 5.5) : `messages.parse` garantit un JSON conforme au schéma et consomme 381 tokens d'entrée contre 654 pour instructor en mode tools (+70 %, définition d'outil envoyée à chaque appel). instructor 1.17 n'a pas de mode pour les structured outputs natifs. La nouvelle tentative sur erreur de validation (seul apport restant d'instructor) est réécrite en ~10 lignes.
- **Parseur SQL : sqlglot** (AST PostgreSQL) au lieu de sqlparse (simple tokenizer).
- SDK `anthropic` 2.8 importé paresseusement (son import prend plusieurs secondes sur cette machine).
- Tests `llm` exécutés uniquement avec `pytest -m llm` (hook dans `conftest.py`) : un `-m` dans `addopts` est remplacé par tout `-m` passé en ligne de commande.

### Reste à faire
- Phases 2 à 6.

### Prochaine étape
- Commit 2.1, puis 2.2 (modèle de sortie `SQLGeneration`), 2.3 guardrails (tests d'abord, sqlglot), 2.4 rôle read-only, 2.5 exécution + EXPLAIN + audit, 2.6 pipeline `python -m text2sql.ask`.

### Blocages
- ~~Docker absent~~ → résolu (2026-11-13) : Docker Desktop 29.9 + WSL 2 installés, PostgreSQL 16 lancé via compose, seed complet chargé en ~24 s, test d'intégration vert. Pour reprendre : lancer Docker Desktop puis `docker compose up -d db` (les données persistent dans le volume `pgdata`).
- ~~Pas de remote~~ → résolu (2026-11-13) : `origin` = https://github.com/Essid01/text2sql-guardrails (privé). Chaque commit est poussé.

# Progress log

## État actuel — 2026-11-14

**Projet terminé : phases 1 à 6.** Pistes possibles ensuite : voir « Pistes » plus bas.

### Pour reprendre
0. Tout en une commande : `docker compose up -d --build` → UI http://localhost:8501, API http://localhost:8010/docs sur cette machine (`API_PORT=8010` dans `.env`).
1. Lancer Docker Desktop, puis `docker compose up -d db` (les données sont conservées dans le volume `pgdata`).
2. Activer l'environnement : `.venv\Scripts\activate` (première fois après ce commit : `pip install -e ".[dev,ui]"`).
3. Vérifier : `pytest -m "not embeddings"` (≈ 15 s, 352 tests) ; `pytest -m llm` pour les 2 tests payants (≈ 0,001 $).
4. Essayer en ligne de commande : `python -m text2sql.ask --no-embedder "Top 3 product categories by net revenue in 2025"`.
5. Essayer l'API et l'interface (deux terminaux) :
   - `uvicorn text2sql.api.app:app --port 8000` → documentation OpenAPI sur http://localhost:8000/docs
   - `streamlit run src/text2sql/ui/app.py` → http://localhost:8501
   - Benchmark : `python -m text2sql.eval` (ou `make eval`) → `reports/latest.md` ; relance gratuite grâce au cache.
   - Sur cette machine le port 8000 est déjà occupé par un autre programme : utiliser `--port 8010` et `API_URL=http://localhost:8010` dans `.env`.

### Fait
- Phase 0 : squelette du dépôt, config typée, CI.
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
- Étape 2.2 : `generation.py` — `SQLGeneration` (sql | clarification | refusal, validateurs de cohérence, descriptions de champs = instructions) + `SQLGenerator`.
- Étape 2.3 : `guardrails/sql_guard.py` — AST sqlglot : 1 seule instruction, racine SELECT (liste autorisée), écritures/verrous/SELECT INTO n'importe où dans l'arbre (CTE incluses), fonctions dangereuses, tables hors schéma (catalogues système inclus), profondeur ≤ 3, longueur max, LIMIT forcé/plafonné, SQL régénéré sans commentaires. 79 tests (attaques → règle attendue, faux positifs, LIMIT).
- Étape 2.4 : `db/roles.py` — rôle `text2sql_reader` (SELECT seul, transactions read-only par défaut, statement_timeout 5 s), recréé par le seed. Tests d'intégration qui contournent les guardrails : la base refuse seule écritures, DDL, accès fichiers, séquences.
- Étape 2.5 : `execution.py` — `QueryExecutor` : transaction read-only toujours annulée, timeout local, `EXPLAIN` d'abord (refus si coût > 100 000), détection de troncature, erreurs typées.
- Étape 2.6 : `pipeline.py` + `audit.py` + `ask.py` — pipeline glossaire → LLM → guardrails → exécution, 5 statuts (`executed`, `clarification`, `refused`, `blocked`, `error`), audit JSONL (`logs/audit.jsonl`), CLI `python -m text2sql.ask "question"`.
- Essai réel (Haiku 5.5, base complète) : 6 questions, 0,002 $ au total ; ~0,0007 $ et ~2 s par question nouvelle (dont ~100 ms SQL) ; cache et glossaire à 0 $.
- CI GitHub verte (lint + unitaires, intégration PostgreSQL).
- Étape 3.1 : `verification/sanity.py` — contrôles gratuits : colonne entièrement NULL, valeurs négatives (montants, quantités), dates hors période, « top N » qui renvoie plus de N lignes, résultat vide/tronqué, tables déclarées par le LLM ≠ tables du SQL, colonnes déclarées inexistantes.
- Étape 3.2 : `verification/backtranslation.py` — traducteur **aveugle** (voit le SQL + schéma des tables utilisées, jamais la question) puis **juge** (voit les deux questions, jamais le SQL ; liste les différences avant de conclure). ≈ 0,0003 $ par vérification.
- Étape 3.3 : `verification/consensus.py` — seconde génération indépendante (zero-shot + consigne différente : pas de cache hit, autre approche), exécutée sous les mêmes guardrails ; résultats comparés comme multiensembles de lignes normalisées, avec repli numérique (colonne de libellé en plus).
- Étape 3.4 : `verification/confidence.py` + `verifier.py` — score composite pondéré (alignement 0,35 ; accord 0,25 ; cohérence 0,20 ; couverture du schéma 0,10 ; confiance déclarée du LLM 0,10), signaux indisponibles exclus, drapeau « hallucination probable » sous 0,6, raisons lisibles. Branché dans le pipeline, l'audit et `ask`.
- Essai réel Phase 3 (5 questions, tous contrôles) : 0,008 $ au total (≈ 0,0016 $/question, ≈ 5–13 s). Ambiguïté réelle détectée par la double génération (« part des commandes en ligne » : tous statuts vs complétées → résultats différents). Un faux positif du juge trouvé et corrigé (voir Décisions).
- Tests : 234 (hors embeddings), tous verts.
- Étape 4.1 : `db/app_schema.sql` + `history.py` — schéma `app` (`query_history`, `feedback`) **sans aucun droit pour le rôle lecteur** (testé : `InsufficientPrivilege`) et rejeté par les guardrails (seul `public` est autorisé). Créé de façon idempotente par le seed et au démarrage de l'API ; le reseed ne touche pas à l'historique. Stores PostgreSQL et mémoire avec le même contrat (mêmes tests sur les deux). Réponse complète stockée en JSONB, lignes plafonnées à 100.
- Étape 4.2 : `pipeline.run_user_sql` — le SQL modifié à la main passe par les **mêmes** guardrails et le même exécuteur (un humain n'est pas plus digne de confiance que le LLM), sans appel LLM ni vérification d'hallucination (`sql_source = "user"`).
- Étape 4.3 : `api/app.py` — FastAPI : `POST /v1/query`, `GET /v1/schema`, `GET /v1/history`, `GET /v1/history/{id}`, `PUT /v1/history/{id}/feedback`, `GET /health`. Le résultat métier est dans `status` (HTTP 200) ; 422/404 seulement pour une requête invalide ou un id inconnu. Entrées bornées (question ≤ 1 000 caractères, SQL ≤ 10 000, page ≤ 200).
- Étape 4.4 : `ui/app.py` + `ui/client.py` — Streamlit : question, boutons de clarification, SQL éditable et ré-exécutable, tableau de résultats, score de confiance détaillé par signal (back-traduction, seconde requête, contrôles), historique dans la barre latérale, schéma, formulaire correct/incorrect. L'UI ne parle qu'à l'API, jamais à la base.
- Étape 4.5 : `feedback.py` — `python -m text2sql.feedback` exporte les réponses marquées incorrectes dans `evals/feedback_candidates.yaml`, avec `status: to_review` : c'est une file de relecture, pas des données de référence.
- Essai réel bout en bout (API sur PostgreSQL + Haiku 5.5) : « Top 3 categories by net revenue in 2025 » → SQL correct, confiance 0,995, 4 appels LLM, 0,002 $ ; `DELETE` et `SELECT * FROM app.query_history` envoyés comme SQL utilisateur → bloqués ; retours, historique et export vérifiés ; interface testée sans navigateur (`streamlit.testing.AppTest`). Lignes d'essai supprimées ensuite.
- Tests : 266 (hors embeddings), tous verts.
- Étape 5.1 : `evals/questions.yaml` — 55 questions écrites à la main (45 EN, 10 FR) : 38 avec SQL de référence (simples, jointures/agrégats, dates, classements), 5 ambiguës, 5 hors périmètre, 7 malveillantes ; 20 requêtes fausses typiques (`bad_sql`). Chaque référence est testée en CI (guardrails + exécution).
- Étape 5.2 : `src/text2sql/eval/` — comparaison des résultats (ordre ignoré, colonnes en plus acceptées, tolérance d'arrondi, entiers exacts, formes alternatives), runner sur le vrai pipeline, requêtes fausses injectées vérifiées à côté de leur référence, compteurs d'écriture PostgreSQL avant/après, rapport Markdown + JSON, `make eval`.
- Étape 5.3 : résultats (détail dans `docs/EVAL_RESULTS.md`) — run 2 : **exactitude d'exécution 100 % (38/38)**, **requêtes fausses signalées 95 % (19/20)**, fausses alertes 3,4 % (2/58), requêtes dangereuses refusées/bloquées 7/7, **0 écriture en base**, clarifications 5/5, questions hors périmètre non répondues 5/5, **≈ 0,0012 $ par question**, latence p50 7,1 s / p95 11,1 s (run 1, tous contrôles). Run 1 : 97,4 % (37/38) ; le seul échec (t04) était une réponse correcte mal notée (année étiquetée par une date), vérifiée à la main → forme alternative ajoutée.
- Étape 5.4 : calibration — AUC par signal : alignement 0,94, accord 0,93, confiance déclarée 0,67, cohérence et couverture 0,5 (jamais déclenchés sur ce jeu). Seuil 0,6 : 95 % / 3,4 % ; 0,8 : 100 % de détection mais 14 % de fausses alertes. **Poids et seuil inchangés** (voir Décisions).
- Tests : 352 (hors embeddings), tous verts.
- Étape 6.1 : `Dockerfile` (python 3.12-slim, utilisateur non-root, sans torch) + `docker-compose.yml` complet : `db`, `seed` (job unique, `--if-empty` : ne recharge jamais des données existantes, met à jour le rôle lecteur et le schéma `app`), `api` (healthcheck), `ui`. Volumes pour les données, le cache LLM et l'audit. Vérifié en local : question réelle → 197 clients en Normandie, 0,0013 $ ; `DROP` bloqué ; UI en HTTP 200.
- Étape 6.2 : CI — actions `checkout` et `setup-python` passées en v7 (Node 24, vérifié dans leur `action.yml`) ; nouveau job `docker` qui démarre toute la stack et vérifie l'API de bout en bout sans appel LLM (SQL utilisateur exécuté, `DROP` et lecture de `app.query_history` bloqués) ; reseed `--if-empty` testé.
- Étape 6.3 : README final — chiffres réels en tête (avec l'historique 97,4 % → 100 %), architecture Mermaid, défense en profondeur, détection d'hallucinations, démarrage en une commande, API, choix de conception, limitations. Fichier `LICENSE` (MIT).

### Décisions
- **Modèle : `claude-haiku-5-5` partout (dev ET éval)** — le moins cher (0,10 $ / 0,50 $ par MTok si prompt ≤ 100K tokens). Configuré dans `.env`, jamais en dur. Effort `low` par défaut.
- Contraintes Haiku 5.5 à respecter en Phase 2 (client LLM) :
  - **Pas de `temperature` / `top_p` / `top_k`** (400) → jamais envoyés (testé) ; le déterminisme vient du cache + structured outputs.
  - **Pas de prefill** assistant (400) → le format JSON passe par structured outputs / tool.
  - **Thinking adaptatif actif par défaut** → lire les blocs par `type`, jamais `content[0]` ; les tokens de thinking comptent dans `max_tokens` (4096 de marge).
  - Ne pas envoyer `budget_tokens` (400). Levier de coût = `output_config.effort`.
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

- Seuil `MAX_QUERY_COST=100000` choisi sur mesure : requêtes analytiques normales 300–4 000 (< 70 ms) ; produits cartésiens / auto-jointures > 700 000 (timeout).
- Chaque appel LLM ≈ 5 300 tokens d'entrée (le schéma de sortie est ajouté par l'API au prompt d'environ 2 300 tokens).
- Le coût estimé trop élevé est classé `blocked` (règle `too_expensive`) ; le timeout et les erreurs SQL sont classés `error` (signal utile pour la Phase 3 : colonne hallucinée → erreur explicite).

- **Détection d'hallucinations : tous les contrôles actifs par défaut** ; chacun désactivable (`VERIFY_BACKTRANSLATION`, `VERIFY_MULTI_QUERY`). Comparaison de la back-traduction par **juge LLM** plutôt que similarité d'embeddings (« CA 2024 » et « CA 2025 » sont presque identiques en embeddings).
- Vérifié sur cas construits : SQL correct → 1,0 ; mauvaise année → 0,0 ; brut au lieu de net → 0,0.
- Faux positif corrigé : le traducteur décrivait « toutes périodes » avec les dates de couverture lues dans les commentaires, le juge y voyait un filtre en trop (SQL correct noté 0,0). Les deux prompts précisent maintenant que la période couverte n'est pas un filtre ; re-vérifié sans régression.
- La seconde génération doit avoir un prompt différent, sinon le cache renvoie la même réponse et l'accord est garanti par construction.
- Limite connue de la comparaison des résultats : un libellé dans une requête et un identifiant numérique dans l'autre → désaccord.
- Poids du score composite = point de départ documenté, **à calibrer en Phase 5** sur le jeu d'évaluation (pas d'ajustement à la main sur quelques exemples).

- Historique : un **verdict par réponse** (le dernier remplace le précédent) ; l'audit JSONL est conservé à côté (append-only, fonctionne sans API, survit à un reset de la base).
- Les réponses incorrectes ne rejoignent **jamais automatiquement** le jeu d'évaluation : la correction d'un utilisateur peut elle-même être fausse ; un humain relit chaque cas et écrit le SQL attendu.
- Endpoints synchrones (`def`) : le pipeline fait des appels bloquants (LLM, PostgreSQL), FastAPI les exécute dans son pool de threads.
- Streamlit en extra optionnel `[ui]` (pandas, pyarrow) : l'API et la CI n'en ont pas besoin. FastAPI, uvicorn et httpx dans les dépendances principales.
- Évaluation : exactitude d'exécution (résultat identique à la référence) et part des requêtes fausses signalées, mesurée surtout sur des erreurs injectées (le modèle en a fait trop peu de naturelles pour un pourcentage). Le coût par question est calculé à partir des tokens, donc stable même quand le cache sert la réponse.
- **Calibration : poids et seuil non modifiés.** Augmenter le poids de l'accord rattraperait la seule requête fausse manquée (j10 : ville « Lyon » au lieu du magasin « Lyon Store », non vue par le juge) mais ferait aussi signaler les 2 fausses alertes (s08, r05), qui ont le même profil. Ajuster sur 20 cas serait du sur-ajustement ; à revoir avec un jeu plus grand.
- Une référence ajoutée après coup (t04) est documentée dans le fichier et dans `docs/EVAL_RESULTS.md` (historique des runs), jamais en silence.
- Limites assumées (à documenter en Phase 6) : benchmark écrit par l'auteur du système sur 7 tables synthétiques (plus facile que Spider/BIRD) ; pas d'authentification ni de limitation de débit sur l'API (démo locale) ; l'historique est partagé par tous les utilisateurs.

### Pistes (hors périmètre initial)
- Agrandir le benchmark (questions écrites par d'autres personnes, schéma plus large) avant toute calibration des poids.
- Authentification et limitation de débit sur l'API ; historique par utilisateur.
- Image Docker optionnelle avec embeddings.

### Prochaine étape
- Rédiger le post LinkedIn avec les chiffres réels ; rendre le dépôt public avant de partager le lien.

### Blocages
- ~~Docker absent~~ → résolu (2026-11-13) : Docker Desktop 29.9 + WSL 2 installés, PostgreSQL 16 lancé via compose, seed complet chargé en ~24 s, test d'intégration vert. Pour reprendre : lancer Docker Desktop puis `docker compose up -d db` (les données persistent dans le volume `pgdata`).
- ~~Pas de remote~~ → résolu (2026-11-13) : `origin` = https://github.com/Essid01/text2sql-guardrails (privé). Chaque commit est poussé.

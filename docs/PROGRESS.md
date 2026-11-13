# Progress log

## État actuel — 2026-11-13

**Phase en cours :** Phase 0 (squelette du dépôt) terminée → prochaine étape : plan de la Phase 1.

### Fait
- Structure du dépôt (layout `src/`), `pyproject.toml` (hatchling, ruff, pytest)
- Configuration typée via `pydantic-settings` (`src/text2sql/config.py`), clé API en `SecretStr`
- `docker-compose.yml` : PostgreSQL 16 avec healthcheck
- `.env.example`, `.gitignore`, CI GitHub Actions (ruff + pytest sans clé API)

### Décisions
- Dépendances ajoutées **phase par phase** (pas de torch/sentence-transformers tant qu'on ne s'en sert pas → CI rapide).
- Marqueurs pytest `integration` (besoin de Postgres) et `llm` (vrai appel API) exclus de la CI.
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

### Reste à faire
- Phases 1 à 6.

### Prochaine étape
- Valider le plan de la Phase 1, puis : schéma retail + seed Faker.

### Blocages
- Docker n'est pas détecté dans le PATH de cette machine (à installer : Docker Desktop).

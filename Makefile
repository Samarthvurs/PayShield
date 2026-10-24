# All commands run inside the `api` container, so the only things you need are Docker and make.
# On Windows without make, use the same targets with: .\make.ps1 <target>

DC  = docker compose
RUN = $(DC) run --rm api

.PHONY: up down data seed train evaluate ingest test lint loadtest demo logs

up:        ## Start Postgres, MongoDB and the API (creates .env from .env.example if missing)
	@test -f .env || cp .env.example .env
	$(DC) up -d --build

down:      ## Stop everything (data is kept in Docker volumes)
	$(DC) down

data:      ## Download the Kaggle dataset into data/raw/
	$(RUN) python -m scripts.download_data

seed:      ## Load cards and payment history into the databases
	$(RUN) python -m scripts.seed

train:     ## Train the fraud model -> models/fraud_model.joblib
	$(RUN) python -m scripts.train_model

evaluate:  ## Evaluate rules vs ML vs combined on fraudTest.csv and the RAG hit rate
	$(RUN) python -m scripts.evaluate
	$(RUN) python -m scripts.evaluate_rag

ingest:    ## Chunk + embed the knowledge base into pgvector
	$(RUN) python -m app.ai.ingest

test:      ## Run tests with coverage
	$(RUN) pytest --cov=app --cov-report=term-missing

lint:      ## Run ruff
	$(RUN) ruff check .

loadtest:  ## 30 users for 60 s against the running API
	$(RUN) python -m loadtest.run

demo:      ## Send ~200 dataset payments through the API
	$(RUN) python -m scripts.demo_traffic

logs:
	$(DC) logs -f api

.PHONY: help install dev migrate makemigration seed seed-admin test lint format up down logs

help:
	@echo "install        - sync dependencies into .venv"
	@echo "up / down      - start / stop local Postgres (docker compose)"
	@echo "migrate        - apply database migrations (alembic upgrade head)"
	@echo "makemigration  - autogenerate a migration: make makemigration m=\"message\""
	@echo "seed           - create a development tenant + owner user + entitlement"
	@echo "seed-admin     - create the platform admin from ADMIN_EMAIL/ADMIN_PASSWORD (idempotent)"
	@echo "dev            - run the API with autoreload"
	@echo "test           - run the test suite"
	@echo "lint           - run ruff checks (lint + format check)"
	@echo "format         - auto-format and auto-fix with ruff"

install:
	uv sync

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

migrate:
	uv run alembic upgrade head

makemigration:
	uv run alembic revision --autogenerate -m "$(m)"

seed:
	uv run python scripts/seed_dev.py

seed-admin:
	uv run python scripts/seed_admin.py

dev:
	uv run uvicorn brain_api.main:app --reload --host 0.0.0.0 --port 8000

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

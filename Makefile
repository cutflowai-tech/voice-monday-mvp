.PHONY: build test lint format check simulate up down logs telephony

build:
	docker compose build api
	docker compose --profile test build tests

test:
	docker compose --profile test run --rm tests pytest -q

lint:
	docker compose --profile test run --rm tests ruff check src tests

format:
	docker compose --profile test run --rm tests ruff format src tests

check: format lint test

simulate:
	docker compose run --rm --no-deps api simulate "حطي تاسك لمحمد إنه يشتري هارد للسيرفر"

up:
	docker compose up -d --build

telephony:
	docker compose --profile telephony up -d --build

down:
	docker compose --profile telephony down

logs:
	docker compose logs -f --tail=100

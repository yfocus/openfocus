# SPDX-License-Identifier: Apache-2.0
.PHONY: serve companion fmt fmt-check lint test test-blackbox check

serve:
	set -a; \
	[ -f .env ] && . ./.env; \
	set +a; \
	poetry run uvicorn openfocus.app:app --host "$${HOST:-$${OPENFOCUS_HOST:-127.0.0.1}}" --port "$${PORT:-$${OPENFOCUS_PORT:-8001}}" --reload

companion:
	set -a; \
	[ -f .env ] && . ./.env; \
	set +a; \
	poetry run python -m openfocus.companion

fmt:
	poetry run ruff format .

fmt-check:
	poetry run ruff format --check .

lint:
	poetry run ruff check .

test:
	poetry run pytest

test-blackbox:
	OPENFOCUS_RUN_BLACKBOX=1 poetry run pytest tests/blackbox -m blackbox

check:
	poetry run ruff format --check .
	poetry run ruff check .
	poetry run pytest

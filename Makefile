# SPDX-License-Identifier: Apache-2.0
.PHONY: serve fmt fmt-check lint test check

HOST ?= 127.0.0.1
PORT ?= 8001

serve:
	poetry run uvicorn openfocus.app:app --host $(HOST) --port $(PORT) --reload

fmt:
	poetry run ruff format .

fmt-check:
	poetry run ruff format --check .

lint:
	poetry run ruff check .

test:
	poetry run pytest

check:
	poetry run ruff format --check .
	poetry run ruff check .
	poetry run pytest

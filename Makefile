.PHONY: serve

HOST ?= 127.0.0.1
PORT ?= 8001

serve:
	poetry run uvicorn openfocus.main:app --host $(HOST) --port $(PORT) --reload


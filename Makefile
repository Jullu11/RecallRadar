.PHONY: setup api gateway web web-build local-check test

# Override when 8000 is busy, e.g. `make api API_PORT=8765` (set BACKEND_URL to match).
API_PORT ?= 8000

setup:
	@test -f .env || cp .env.example .env
	@python3 -m venv .venv
	@.venv/bin/python -m pip install -r requirements.txt
	@npm --prefix gateway ci
	@npm --prefix frontend ci || npm --prefix frontend install
	@echo "Local dependencies are installed. Edit .env, apply the Supabase migration, then run 'make api', 'make gateway', and optionally 'make web'."

api:
	@.venv/bin/uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port $(API_PORT)

gateway:
	@npm --prefix gateway run dev

web:
	@API_PORT=$(API_PORT) npm --prefix frontend run dev

web-build:
	@npm --prefix frontend run build

test:
	@DEMO_MODE=true PYTHONPATH=backend .venv/bin/python -m unittest discover -s tests -v

local-check:
	@.venv/bin/python -m compileall -q backend/app
	@cd gateway && npm exec -- tsc --noEmit
	@cd frontend && npm exec -- tsc --noEmit
	@$(MAKE) test

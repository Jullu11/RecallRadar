.PHONY: setup api gateway local-check test

setup:
	@test -f .env || cp .env.example .env
	@python3 -m venv .venv
	@.venv/bin/python -m pip install -r requirements.txt
	@npm --prefix gateway ci
	@echo "Local dependencies are installed. Edit .env, apply the Supabase migration, then run 'make api' and 'make gateway' in separate terminals."

api:
	@.venv/bin/uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000

gateway:
	@npm --prefix gateway run dev

test:
	@DEMO_MODE=true PYTHONPATH=backend .venv/bin/python -m unittest discover -s tests -v

local-check:
	@.venv/bin/python -m compileall -q backend/app
	@cd gateway && npm exec -- tsc --noEmit
	@$(MAKE) test

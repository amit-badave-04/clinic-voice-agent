# Common entry points. On Windows without make, run the underlying commands directly
# (they are plain python/flyctl invocations — see README "Reproduce" section).

.PHONY: dev migrate seed-local seed-cliniko agent-sync deploy test eval

dev:
	uvicorn app.main:app --reload --port 8080

migrate:
	alembic upgrade head

seed-local:
	python -m seed.local_seed

seed-cliniko:
	python -m seed.cliniko_seed

agent-sync:
	python -m agent.agent_config sync

deploy:
	flyctl deploy

test:
	pytest tests -q

eval:
	python -m evals.run_evals

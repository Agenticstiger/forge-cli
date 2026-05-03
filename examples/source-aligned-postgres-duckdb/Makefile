SHELL := /bin/bash

VENV ?= ../../.venv
PY   := $(VENV)/bin/python

.PHONY: help up down clean run verify all

help:
	@echo "Targets:"
	@echo "  up      — start Postgres container with seed data"
	@echo "  run     — fluid validate -> apply (acquisition)"
	@echo "  verify  — assert row count + schema in the output Parquet"
	@echo "  down    — stop containers"
	@echo "  clean   — remove output + state"
	@echo "  all     — up + run + verify"

up:
	docker compose up -d
	@echo "Waiting for Postgres to be healthy..."
	@for i in {1..30}; do \
	  status=$$(docker inspect -f '{{.State.Health.Status}}' fluid-source-aligned-pg 2>/dev/null || echo "starting"); \
	  if [ "$$status" = "healthy" ]; then echo "Postgres healthy"; break; fi; \
	  sleep 1; \
	done

down:
	docker compose down

clean:
	rm -rf ./out ./.fluid

run: clean
	@echo "→ fluid validate"
	$(PY) -m fluid_build.cli validate contract.fluid.yaml
	@echo "→ fluid apply (acquisition)"
	$(PY) -m fluid_build.cli apply --build ingest_orders contract.fluid.yaml

verify:
	$(PY) verify.py

all: up run verify

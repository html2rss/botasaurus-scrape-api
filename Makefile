.PHONY: test build serve health scrape-example smoke lint lintfix check ready openapi openapi-verify spectral typecheck

.DEFAULT_GOAL := check

IMAGE ?= botasaurus-api
PORT ?= 4010
BASE_URL ?= http://localhost:$(PORT)
OPENAPI_FILE ?= openapi.yaml
SPECTRAL_IMAGE ?= stoplight/spectral:6

PYTHON ?= $(shell if [ -f .venv/bin/python3 ]; then echo .venv/bin/python3; else which python3; fi)
RUFF ?= $(shell if [ -f .venv/bin/ruff ]; then echo .venv/bin/ruff; else which ruff; fi)

lint:
	$(RUFF) check .
	$(RUFF) format --check .
	docker run --rm -i hadolint/hadolint < Dockerfile
	$(MAKE) spectral

lintfix:
	$(RUFF) check --fix .
	$(RUFF) format .

spectral:
	docker run --rm -e DO_NOT_TRACK=1 \
		-v "$(CURDIR)":/spec:ro $(SPECTRAL_IMAGE) \
		lint --fail-severity=warn --ruleset /spec/.spectral.yaml /spec/openapi.yaml

openapi:
	$(PYTHON) scripts/export_openapi.py --out $(OPENAPI_FILE)

openapi-verify:
	@tmp=$$(mktemp); \
	trap 'rm -f $$tmp' EXIT; \
	$(PYTHON) scripts/export_openapi.py --out $$tmp && \
	diff -u $(OPENAPI_FILE) $$tmp

check: lint test typecheck openapi-verify

ready: check

test:
	$(PYTHON) -m unittest discover -s tests

typecheck:
	$(PYTHON) -m pyright app tests


GIT_SHA ?= $(shell git rev-parse HEAD 2>/dev/null || echo unknown)

build:
	docker build --build-arg GIT_SHA=$(GIT_SHA) -t $(IMAGE) .

serve: build
	docker run --rm -p $(PORT):4010 \
		--init --shm-size=1gb \
		--read-only --cap-drop ALL --security-opt no-new-privileges:true \
		--tmpfs /tmp:size=512m \
		--tmpfs /home/appuser:size=128m,uid=1000,gid=1000,mode=700 \
		--memory 2g \
		$(IMAGE)

health:
	curl -s $(BASE_URL)/health

scrape-example:
	curl -s -X POST $(BASE_URL)/scrape \
		-H 'Content-Type: application/json' \
		-d '{"url":"https://example.com"}'

smoke:
	./scripts/smoke.sh

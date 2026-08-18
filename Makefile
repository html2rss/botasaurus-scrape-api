.PHONY: test build serve health scrape-example smoke lint lintfix check ready openapi openapi-verify

.DEFAULT_GOAL := check

IMAGE ?= botasaurus-api
PORT ?= 4010
BASE_URL ?= http://localhost:$(PORT)
OPENAPI_FILE ?= openapi.yaml

lint:
	ruff check .
	ruff format --check .
	docker run --rm -i hadolint/hadolint < Dockerfile

lintfix:
	ruff check --fix .
	ruff format .

openapi:
	python3 scripts/export_openapi.py --out $(OPENAPI_FILE)

openapi-verify:
	@tmp=$$(mktemp) && \
	python3 scripts/export_openapi.py --out $$tmp && \
	diff -u $(OPENAPI_FILE) $$tmp && \
	rm -f $$tmp

check: lint test openapi-verify

ready: check

test:
	python3 -m unittest discover -s tests

build:
	docker build -t $(IMAGE) .

serve: build
	docker run --rm -p $(PORT):4010 $(IMAGE)

health:
	curl -s $(BASE_URL)/health

scrape-example:
	curl -s -X POST $(BASE_URL)/scrape \
		-H 'Content-Type: application/json' \
		-d '{"url":"https://example.com"}'

smoke:
	./scripts/smoke.sh

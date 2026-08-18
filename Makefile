.PHONY: test build serve health scrape-example smoke lint lintfix check ready

.DEFAULT_GOAL := check

IMAGE ?= botasaurus-api
PORT ?= 4010
BASE_URL ?= http://localhost:$(PORT)

lint:
	ruff check .
	ruff format --check .
	docker run --rm -i hadolint/hadolint < Dockerfile

lintfix:
	ruff check --fix .
	ruff format .

check: lint test

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

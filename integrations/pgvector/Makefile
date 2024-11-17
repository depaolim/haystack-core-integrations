PYTHON_BASE := ~/.pyenv/versions/3.13.0/bin/python
PYTHON_VENV := ./venv/bin/python
PYTHON_PIP := $(PYTHON_VENV) -m pip
HATCH := ./venv/bin/hatch
PGNAME := haystack-integrations-pgvector

# no good:
#	PgvectorKeywordRetriever.run -> docs = self.document_store._connected__embedding_retrieval
#	PgvectorEmbeddingRetriever.run -> docs = self.document_store._connected__embedding_retrieval
# autocommit = False
# create PR with test case??
# 	* localize delta and apply again
# (initialize schema in __init__, if preferred)

all: venv setup test cov lint teardown

venv:
	$(PYTHON_BASE) -m venv venv
	$(PYTHON_PIP) install --upgrade pip
	$(PYTHON_PIP) install -e .
	$(PYTHON_PIP) install -r ../../requirements.txt
	$(PYTHON_PIP) install pytest

setup:
	docker run -d -p 5432:5432 -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=postgres --name $(PGNAME) ankane/pgvector

teardown:
	docker stop $(PGNAME)
	docker container rm -f haystack-integrations-pgvector

test:
	$(HATCH) run test

cov:
	$(HATCH) run cov

lint:
	$(HATCH) run lint:all

clean: teardown
	rm -rf venv
	docker image rm -f ankane/pgvector:latest

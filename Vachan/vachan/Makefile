# Vachan — local development.
# Nothing here needs Docker, a cloud account or an API key.
#
# On Windows without `make`, run the command in the right-hand comment.

.PHONY: help demo init-db ingest sweep scores app test lint

help:
	@echo "make demo      # seed the sample conversation and run the whole pipeline"
	@echo "make init-db   # create the schema (SQLite by default)"
	@echo "make ingest F=~/Downloads/WhatsApp\\ Chat.txt"
	@echo "make sweep     # age overdue promises, draft chases, rescore (cron)"
	@echo "make scores    # vendor reliability"
	@echo "make app       # the zero-install browser app (just open the file)"
	@echo "make test      # the Python tests — stdlib only, no install needed"
	@echo "make lint      # optional: needs pip install ruff"

# cd must stay on the same line as the command: make gives each line its own shell.
demo:
	cd agent && python -m vachan demo

init-db:
	cd agent && python -m vachan init-db

ingest:
	cd agent && python -m vachan ingest "$(F)"

sweep:
	cd agent && python -m vachan sweep

scores:
	cd agent && python -m vachan scores

app:
	@echo "Open this file in a browser: vachan/app/index.html"
	@echo "It needs no server and no install. Use it when you cannot run Python."

# unittest, not pytest, so the default path works on a machine with nothing
# installed. pytest runs these same tests when it is present.
test:
	cd agent && python -m unittest discover -s tests -v

lint:
	cd agent && python -m ruff check vachan

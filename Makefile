.PHONY: install
install: ## Install the virtual environment and install the pre-commit hooks
	@echo "🚀 Creating virtual environment using uv"
	@uv sync
	@uv run pre-commit install

.PHONY: check
check: ## Run code quality tools.
	@echo "🚀 Checking lock file consistency with 'pyproject.toml'"
	@uv lock --locked
	@echo "🚀 Linting code: Running pre-commit"
	@uv run pre-commit run -a
	@echo "🚀 Static type checking: Running ty"
	@uv run ty check
	@echo "🚀 Checking for obsolete dependencies: Running deptry"
	@uv run deptry app

.PHONY: test
test: ## Test the code with pytest
	@echo "🚀 Testing code: Running pytest"
	@uv run python -m pytest --cov --cov-config=pyproject.toml --cov-report=xml

.PHONY: dev
dev: ## Run the API locally with autoreload
	@uv run fastapi dev app/main.py

PRIVATE_DATA ?= data-prep/private-data
DATA_DIR ?= data-prep/naomi-data
# The Spectrum/SHIPP extractor's R packages. SpectrumUtils uses reshape2 and
# lubridate without declaring them, so they are listed here for it.
R_PACKAGES = dplyr tibble tidyr readr openxlsx arrow reshape2 lubridate rlglaubius/SpectrumUtils

.PHONY: data
data: ## Rebuild the dataset: the committed demo data, plus data-prep/private-data if checked out
	@rm -rf $(DATA_DIR)
	@uv run --script data-prep/extract_indicators.py data-prep/raw-data/datasets.yaml $(wildcard $(PRIVATE_DATA)/datasets.yaml) --out-dir $(DATA_DIR)

.PHONY: r-deps
r-deps: ## Install the R packages needed to build Spectrum and SHIPP data
	@Rscript -e 'repos <- getOption("repos"); if (is.null(repos) || identical(unname(repos["CRAN"]), "@CRAN@")) options(repos = c(CRAN = "https://cloud.r-project.org"))' \
		-e 'if (!requireNamespace("pak", quietly = TRUE)) install.packages("pak")' \
		-e 'pak::pak(strsplit("$(R_PACKAGES)", " ")[[1]])'

.PHONY: docker
docker: data ## Build the image, serving the dataset `make data` builds
	@docker build --build-context data=$(DATA_DIR) -t hivtools-mcp .

.PHONY: docs-test
docs-test: ## Test if documentation can be built without warnings or errors
	@uv run mkdocs build -s

.PHONY: docs
docs: ## Build and serve the documentation
	@uv run mkdocs serve

.PHONY: help
help:
	@uv run python -c "import re; \
	[[print(f'\033[36m{m[0]:<20}\033[0m {m[1]}') for m in re.findall(r'^([a-zA-Z_-]+):.*?## (.*)$$', open(makefile).read(), re.M)] for makefile in ('$(MAKEFILE_LIST)').strip().split()]"

.DEFAULT_GOAL := help

# Universal Mail Automation — common tasks. `make deploy` is the one-command deploy.
# All deploy targets delegate to scripts/deploy.sh (see DEPLOY.md).

.DEFAULT_GOAL := help
.PHONY: help deploy deploy-image deploy-cloudflare deploy-render docker-build test

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

deploy: ## Build + run the app locally and smoke-test it (one-command deploy)
	@bash scripts/deploy.sh docker

deploy-image: ## Build and push the image (set IMAGE=ghcr.io/<owner>/<repo>:latest)
	@bash scripts/deploy.sh image

deploy-cloudflare: ## Deploy the read-only demo Worker (needs CLOUDFLARE_API_TOKEN)
	@bash scripts/deploy.sh cloudflare

deploy-render: ## Trigger a Render deploy (needs RENDER_DEPLOY_HOOK) or print setup steps
	@bash scripts/deploy.sh render

docker-build: ## Build the production image only
	@docker build -t $${IMAGE:-mail-api:local} .

test: ## Run the offline test suite
	@python3 -m pytest -q

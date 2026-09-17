# The token every API and MCP request must carry, as an X-API-Key header (see
# app/auth.py).
# Generated here so it never has to be made up or passed around by hand. Read it
# with `terraform output -raw api_token`; rotate it with
# `terraform apply -replace=random_password.api_token`, then update the API_TOKEN
# secret in GitHub and the claude.ai connector.
resource "random_password" "api_token" {
  length  = 48
  special = false
}

# Two user-assigned managed identities, no Microsoft Entra *application*
# registration involved anywhere. That matters: registering an Entra app needs a
# separate directory permission that subscription Owner does not grant, and not
# every deployer has it. A federated credential on a user-assigned identity gets
# the same GitHub OIDC login with only ARM/RBAC permissions (which Owner does
# grant), so `terraform apply` doesn't depend on anyone's Entra role.

# Identity the Container App pulls images with.
resource "azurerm_user_assigned_identity" "container_app_pull" {
  name                = "id-${var.name_prefix}-pull"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "container_app_acr_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.container_app_pull.principal_id
}

# Identity GitHub Actions authenticates as via OIDC, scoped to exactly what the
# release workflow does: push to this registry, roll a new revision of this app.
resource "azurerm_user_assigned_identity" "github_actions" {
  name                = "id-${var.name_prefix}-github"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "github_actions" {
  name                = "github-${var.github_environment}"
  resource_group_name = azurerm_resource_group.main.name
  parent_id           = azurerm_user_assigned_identity.github_actions.id
  issuer              = "https://token.actions.githubusercontent.com"
  audience            = ["api://AzureADTokenExchange"]
  # Classic federated credentials (Entra app or managed identity, same rule) take
  # an exact subject, no tag wildcards - restrict which tags may deploy with the
  # GitHub environment's own deployment tag policy (v*) instead.
  subject = "repo:${var.github_repository}:environment:${var.github_environment}"
}

resource "azurerm_role_assignment" "github_acr_push" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}

resource "azurerm_role_assignment" "github_containerapp" {
  scope                = azurerm_container_app.main.id
  role_definition_name = "Container Apps Contributor"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}

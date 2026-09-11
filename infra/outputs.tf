output "container_app_fqdn" {
  description = "Public hostname of the app. Set as the CONTAINER_APP_FQDN repo variable."
  value       = azurerm_container_app.main.ingress[0].fqdn
}

output "container_app_url" {
  description = "Public URL of the app."
  value       = "https://${azurerm_container_app.main.ingress[0].fqdn}"
}

output "container_app_name" {
  description = "Set as the CONTAINER_APP_NAME repo variable."
  value       = azurerm_container_app.main.name
}

output "resource_group_name" {
  description = "Set as the RESOURCE_GROUP repo variable."
  value       = azurerm_resource_group.main.name
}

output "container_app_environment_id" {
  description = "For a future Azure Files volume for real data."
  value       = azurerm_container_app_environment.main.id
}

output "container_registry_name" {
  description = "Set as the ACR_NAME repo variable."
  value       = azurerm_container_registry.main.name
}

output "container_registry_login_server" {
  description = "Set as the ACR_LOGIN_SERVER repo variable."
  value       = azurerm_container_registry.main.login_server
}

output "github_actions_client_id" {
  description = "Set as the AZURE_CLIENT_ID repo variable (not a secret - it's a public client ID used with OIDC)."
  value       = azurerm_user_assigned_identity.github_actions.client_id
}

output "azure_tenant_id" {
  description = "Set as the AZURE_TENANT_ID repo variable."
  value       = data.azurerm_client_config.current.tenant_id
}

output "azure_subscription_id" {
  description = "Set as the AZURE_SUBSCRIPTION_ID repo variable."
  value       = data.azurerm_client_config.current.subscription_id
}

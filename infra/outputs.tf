output "container_app_fqdn" {
  description = "Public hostname of the app. Set as the CONTAINER_APP_FQDN repo variable."
  value       = azurerm_container_app.main.ingress[0].fqdn
}

output "container_app_url" {
  description = "Public URL of the app."
  value       = "https://${azurerm_container_app.main.ingress[0].fqdn}"
}

output "custom_domain_url" {
  description = "Public URL of the app on its custom domain."
  value       = local.custom_domain_enabled ? "https://${var.custom_domain}" : null
}

output "custom_domain_dns_records" {
  description = "Records to create in Cloudflare before applying custom_domain. DNS only (grey cloud), not proxied: Azure checks that the A record points straight at the app."
  # The environment's verification ID is the app's, minus the sensitive flag the
  # provider puts on the app's copy (it ends up in public DNS anyway).
  value = local.custom_domain_enabled ? [
    { type = "A", name = var.custom_domain, content = azurerm_container_app_environment.main.static_ip_address },
    { type = "TXT", name = "asuid.${var.custom_domain}", content = azurerm_container_app_environment.main.custom_domain_verification_id },
  ] : []
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

output "api_token" {
  description = "Token for the API and MCP. Set as the API_TOKEN secret in the production environment, and as the claude.ai connector's authorization header (Bearer <token>)."
  value       = random_password.api_token.result
  sensitive   = true
}

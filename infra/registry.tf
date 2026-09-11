# ACR names are globally unique across Azure and alphanumeric-only, so a random
# suffix avoids a manual retry-if-taken loop on first apply.
resource "random_string" "acr_suffix" {
  length  = 4
  special = false
  upper   = false
}

locals {
  container_registry_name = coalesce(
    var.container_registry_name,
    "acr${replace(var.name_prefix, "-", "")}${random_string.acr_suffix.result}"
  )
}

resource "azurerm_container_registry" "main" {
  name                = local.container_registry_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = var.tags
}

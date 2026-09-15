data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "main" {
  name     = var.resource_group_name
  location = var.location
  tags     = var.tags
}

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${var.name_prefix}-prod"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  daily_quota_gb      = var.log_daily_quota_gb
  tags                = var.tags
}

resource "azurerm_container_app_environment" "main" {
  name                = "cae-${var.name_prefix}-prod"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  # Not "log-analytics": that destination authenticates with the workspace shared
  # key and ingests via the retired HTTP Data Collector API into the legacy
  # ContainerAppConsoleLogs_CL table. azure-monitor routes the same logs through
  # the diagnostic setting below instead.
  logs_destination = "azure-monitor"
  tags             = var.tags
}

resource "azurerm_monitor_diagnostic_setting" "container_app_environment" {
  name                       = "logs-to-${azurerm_log_analytics_workspace.main.name}"
  target_resource_id         = azurerm_container_app_environment.main.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  # Resource-specific tables (ContainerAppConsoleLogs, column `Log`) rather than
  # the catch-all AzureDiagnostics table.
  log_analytics_destination_type = "Dedicated"

  enabled_log {
    category = "ContainerAppConsoleLogs"
  }

  enabled_log {
    category = "ContainerAppSystemLogs"
  }
}

resource "azurerm_container_app" "main" {
  name                         = "ca-${var.name_prefix}-prod"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.container_app_pull.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.container_app_pull.id
  }

  secret {
    name  = "api-token"
    value = random_password.api_token.result
  }

  ingress {
    external_enabled = true
    target_port      = 80
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }

    # Empty var => no restrictions => open. Any Allow rule flips ingress to default-deny.
    dynamic "ip_security_restriction" {
      for_each = var.allowed_ingress_cidrs
      content {
        name             = "allow-${ip_security_restriction.key}"
        ip_address_range = ip_security_restriction.value
        action           = "Allow"
      }
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    http_scale_rule {
      name                = "http-concurrency"
      concurrent_requests = tostring(var.http_concurrency)
    }

    container {
      name   = "hivtools-mcp"
      image  = var.container_image
      cpu    = var.cpu
      memory = var.memory

      env {
        name        = "HIVTOOLS_MCP_API_TOKEN"
        secret_name = "api-token"
      }
      env {
        name  = "HIVTOOLS_MCP_DUCKDB_THREADS"
        value = "1"
      }
      env {
        name  = "HIVTOOLS_MCP_ENABLE_DOCS"
        value = "false"
      }
      env {
        name  = "HIVTOOLS_MCP_LOG_JSON"
        value = "true"
      }

      liveness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 80
      }

      readiness_probe {
        transport = "HTTP"
        path      = "/health/ready"
        port      = 80
      }

      startup_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 80
      }
    }
  }

  lifecycle {
    # The release workflow rolls new images via `az containerapp update`; Terraform
    # owns every other setting. Without this, every plan after a deploy would try to
    # revert the image to var.container_image.
    ignore_changes = [template[0].container[0].image]
  }

  # The pull identity needs AcrPull before the app can actually use it to pull.
  depends_on = [azurerm_role_assignment.container_app_acr_pull]
}

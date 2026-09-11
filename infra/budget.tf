# Alert-only cost guard. Azure budgets never stop spend - they email when a
# threshold is crossed, which is enough to catch a bot-driven cost spike early.

resource "azurerm_consumption_budget_resource_group" "main" {
  name              = "budget-${var.name_prefix}-prod"
  resource_group_id = azurerm_resource_group.main.id
  amount            = var.budget_amount
  time_grain        = "Monthly"

  time_period {
    start_date = var.budget_start_date
  }

  dynamic "notification" {
    for_each = toset([50, 80, 100])
    content {
      enabled        = true
      threshold      = notification.value
      operator       = "GreaterThan"
      threshold_type = "Actual"
      contact_emails = [var.budget_alert_email]
    }
  }

  notification {
    enabled        = true
    threshold      = 90
    operator       = "GreaterThan"
    threshold_type = "Forecasted"
    contact_emails = [var.budget_alert_email]
  }

  lifecycle {
    ignore_changes = [time_period]
  }
}

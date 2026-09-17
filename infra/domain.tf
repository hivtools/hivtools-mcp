# Custom domain with a free Azure-managed certificate. DNS is on Cloudflare and
# set by hand (`terraform output custom_domain_dns_records`). The records must
# exist before this applies: Azure checks them when the hostname is added, and
# again when it issues the certificate. See infra/README.md.

locals {
  custom_domain_enabled = var.custom_domain != ""
}

resource "azurerm_container_app_custom_domain" "main" {
  count            = local.custom_domain_enabled ? 1 : 0
  name             = var.custom_domain
  container_app_id = azurerm_container_app.main.id

  lifecycle {
    # The binding below sets these outside Terraform.
    ignore_changes = [certificate_binding_type, container_app_environment_certificate_id]
  }
}

# An apex domain can only be validated over HTTP (CNAME validation is for
# subdomains), so issuing and renewing the certificate needs the app open to the
# internet - it fails if allowed_ingress_cidrs is set.
resource "azurerm_container_app_environment_managed_certificate" "main" {
  count                        = local.custom_domain_enabled ? 1 : 0
  name                         = "cert-${replace(var.custom_domain, ".", "-")}"
  container_app_environment_id = azurerm_container_app_environment.main.id
  subject_name                 = var.custom_domain
  domain_control_validation    = "HTTP"
  tags                         = var.tags

  # Azure only issues a certificate for a hostname already on an app.
  depends_on = [azurerm_container_app_custom_domain.main]
}

# Neither resource above attaches the certificate to the hostname, and the
# provider has no resource that does, so az does it (deployers already need az,
# see README). On destroy it removes the hostname first, as Azure won't delete a
# certificate that is still bound.
resource "terraform_data" "custom_domain_binding" {
  count = local.custom_domain_enabled ? 1 : 0

  triggers_replace = [
    azurerm_container_app_custom_domain.main[0].id,
    azurerm_container_app_environment_managed_certificate.main[0].id,
  ]

  input = {
    app_id      = azurerm_container_app.main.id
    hostname    = var.custom_domain
    certificate = azurerm_container_app_environment_managed_certificate.main[0].id
  }

  provisioner "local-exec" {
    command = "az containerapp hostname bind --ids ${self.input.app_id} --hostname ${self.input.hostname} --certificate ${self.input.certificate} --output none"
  }

  provisioner "local-exec" {
    when    = destroy
    command = "az containerapp hostname delete --ids ${self.input.app_id} --hostname ${self.input.hostname} --yes --output none"
  }
}

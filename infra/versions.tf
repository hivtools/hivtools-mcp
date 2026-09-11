terraform {
  required_version = "~> 1.9"

  # State is local by default (infra/terraform.tfstate, gitignored). See infra/README.md
  # for moving to a shared remote backend.

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.20"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

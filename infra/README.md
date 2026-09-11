# infra/ — Azure deployment

Terraform for the production Azure Container Apps deployment: resource group, Log
Analytics, an Azure Container Registry, Container Apps environment + app, a budget
alert, and two user-assigned managed identities (one for the Container App to pull
images, one for GitHub Actions to push images and roll revisions via OIDC — no
Entra *application* registration involved, just ARM role assignments, so this
never hits the Entra-directory-permission wall a plain Entra-app OIDC setup can).

State is **local** (`infra/terraform.tfstate`, gitignored). Terraform is run by
hand — GitHub Actions never applies infra, it only builds/pushes an image and
rolls a Container Apps revision (`.github/workflows/on-release-main.yml`).

## First-time provisioning

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # set subscription_id
az login
terraform init
terraform apply
```

This comes up on a placeholder image (`mcr.microsoft.com/k8se/quickstart-full`);
`min_replicas = 0` means it never actually runs until the first release, so
that's fine.

`container_app.tf` has `lifecycle { ignore_changes = [... .image] }` on purpose,
so the release workflow's `az containerapp update` always wins and a routine
`terraform apply` can never revert a deploy back to `var.container_image`. The
flip side: **once the app exists, re-running `terraform apply` with a different
`-var container_image=...` will not change the running image** - Terraform is
told to ignore that field entirely, not just re-apply it. To get a real image
running before your first release, swap it with `az` directly, not Terraform:

```bash
terraform output   # for the registry name/login server below

az acr login --name <container_registry_name>
docker build -t <container_registry_login_server>/hivtools-mcp:bootstrap ..
docker push <container_registry_login_server>/hivtools-mcp:bootstrap
az containerapp update \
  --name <container_app_name> --resource-group <resource_group_name> \
  --image <container_registry_login_server>/hivtools-mcp:bootstrap
```

Or just skip this and cut your first GitHub Release once GitHub is wired up
below - it does exactly the above, via CI.

### Wire up GitHub

Create the **`production`** environment in the repo (set its deployment tag policy
to `v*`), then add these repo **variables** (Settings → Secrets and variables →
Actions → Variables) from `terraform output`:

| Variable | Output |
|---|---|
| `AZURE_CLIENT_ID` | `github_actions_client_id` |
| `AZURE_TENANT_ID` | `azure_tenant_id` |
| `AZURE_SUBSCRIPTION_ID` | `azure_subscription_id` |
| `ACR_NAME` | `container_registry_name` |
| `ACR_LOGIN_SERVER` | `container_registry_login_server` |
| `RESOURCE_GROUP` | `resource_group_name` |
| `CONTAINER_APP_NAME` | `container_app_name` |
| `CONTAINER_APP_FQDN` | `container_app_fqdn` |

Then bump `../pyproject.toml` to the release version and cut a GitHub Release
`vX.Y.Z` — `deploy-docs` and `deploy-azure` run.

## Setting up as a new deployer

```bash
az login
az account set --subscription <id>
```

You need Owner (or Contributor + User Access Administrator) on the subscription. State is local, so get `infra/terraform.tfstate` from whoever holds it before you apply (or ask to move state to a remote backend if more than one person needs this regularly).

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan   # should show no changes against the existing state
```

## Deploying a change

```bash
# edit .tf files
terraform fmt
terraform plan
terraform apply
```

CI (`main.yml`) only runs `terraform fmt -check` + `validate` on PRs — nothing
applies automatically. Whoever merges applies by hand. If the change affects an
output GitHub reads (FQDN, app name, etc.), update the matching repo variable too.

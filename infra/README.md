# infra/ — Azure deployment

Terraform for the production Azure Container Apps deployment: resource group, Log
Analytics, an Azure Container Registry, Container Apps environment + app on a
custom domain, a budget alert, and two user-assigned managed identities (one for the Container App to pull
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
terraform apply -var custom_domain=
```

The custom domain is left out of this first apply because its DNS records need
the environment's IP address, which doesn't exist yet. Add it afterwards (see
[Custom domain](#custom-domain)).

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

The release also needs the private data and the API token. Add these to the
**`production` environment** (Settings → Environments → production), not the
repo, so that only release runs can read them - this repo is public:

| Name | Kind | Value |
|---|---|---|
| `DATA_REPO` | variable | `owner/repo` of the private data repo: `hivtools/hivtools-mcp-data` |
| `DATA_REPO_REF` | variable | Optional: branch, tag or commit of it to build from (default branch if unset) |
| `DATA_REPO_DEPLOY_KEY` | secret | Private half of a read-only deploy key on the data repo |
| `API_TOKEN` | secret | `terraform output -raw api_token`, for the smoke test |

To make the deploy key: `ssh-keygen -t ed25519 -N "" -f data-repo-key`, add
`data-repo-key.pub` to the data repo (Settings → Deploy keys, read-only), paste
`data-repo-key` into `DATA_REPO_DEPLOY_KEY`, then delete both files.

The data repo holds a `datasets.yaml` at its root listing each country's input
files (format in `data-prep/raw-data/datasets.yaml`). It is checked out at
`data-prep/private-data`, both in the release and locally.

Then bump `../pyproject.toml` to the release version and cut a GitHub Release
`vX.Y.Z` — `deploy-docs` and `deploy-azure` run.

## Custom domain

The app is served on `hivtools.org` (`var.custom_domain`) with a free
Azure-managed certificate, which Azure renews by itself. The domain is
registered at Cloudflare and its DNS is managed there by hand, not by Terraform.

Azure checks the DNS records before it will add the domain, so they must exist
before the apply that adds it:

1. `terraform apply -refresh-only` to show the records (the
   `custom_domain_dns_records` output) without changing anything.
2. In Cloudflare (hivtools.org → DNS → Records), create them as given: an `A`
   record for the domain and a `TXT` record for `asuid.<domain>`. Set the `A`
   record to **DNS only** (grey cloud): if Cloudflare proxies it, the domain
   resolves to Cloudflare, not Azure, and Azure's check fails.
3. `terraform apply`. Issuing the certificate can take up to 20 minutes.

Azure issues and renews the certificate by fetching a file from the domain over
plain HTTP. Any `allowed_ingress_cidrs` restriction blocks that, so keep the app
open while it has a custom domain.

The provider can't attach the managed certificate to the domain, so
`terraform_data.custom_domain_binding` runs `az containerapp hostname bind` to do
that. Run Terraform somewhere `az` is logged in to this subscription.

Once the domain serves the app, point clients (the claude.ai connector) at it.
The `CONTAINER_APP_FQDN` variable can stay as it is: the smoke test works on
either hostname.

To remove the domain, set `custom_domain = ""` and apply, then delete the DNS
records.

## Authentication

The app requires an API token (see the main README). Terraform generates it
(`random_password.api_token`) and hands it to the Container App as a secret. The
image refuses to start without it, so **apply Terraform before releasing an image
that requires it**. Older images ignore the extra variable, so applying first is
always safe.

Clients need the token too:

- **claude.ai connector**: an organisation admin sets a request header
  `authorization` with the value `Bearer <token>` on the custom connector
  (static request headers are a beta feature of custom connectors). Claude sends
  the value exactly as entered, so it must include `Bearer `.
- **GitHub**: the `API_TOKEN` environment secret above.

To rotate it: `terraform apply -replace=random_password.api_token`, then update
both of the above. Requests with the old token fail from the moment the new
revision is live.

The token is in Terraform state, which is local and gitignored - keep it that way.

## Logs

The app's output goes to the Log Analytics workspace `log-hivtools-mcp-prod`. To
read it, you need access to the subscription. In the portal, open the workspace
(or the Container App) and choose **Logs**, then switch the query editor to
**KQL mode**. Logs take a minute or two to arrive.

Every request, with its status code (health probes left out):

```kusto
ContainerAppConsoleLogs
| where TimeGenerated > ago(1h)
| where Log has "HTTP/1." and Log !has "/health"
| project TimeGenerated, Log
| order by TimeGenerated desc
```

The app's own log lines are JSON, with their fields under `record.extra`.
Requests refused for a missing or wrong bearer token, with the names (not
values) of the headers they carried:

```kusto
ContainerAppConsoleLogs
| where TimeGenerated > ago(1h)
| extend extra = parse_json(Log).record.extra
| where extra.event == "auth_refused"
| project TimeGenerated, method = tostring(extra.method), path = tostring(extra.path),
    reason = tostring(extra.reason), headers = extra.headers
| order by TimeGenerated desc
```

MCP tool calls: set `extra.event == "tool_call"` instead. Those lines hold the
arguments and the start of each response, so they can include data that isn't
public; don't copy them anywhere public.

From the command line, pass any of these queries to:

```bash
az monitor log-analytics query --analytics-query '<query>' -o table \
  -w "$(az monitor log-analytics workspace show --subscription <subscription id> \
    -g rg-hivtools-mcp-prod -n log-hivtools-mcp-prod --query customerId -o tsv)"
```

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

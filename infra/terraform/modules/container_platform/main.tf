terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

variable "project_id" { type = string }
variable "region" { type = string }
variable "env" { type = string }
variable "subnet_id" { type = string }
variable "images" {
  type        = map(string)
  description = "Container image per service: approval_hub, gateway, worker."
}
variable "platform_secret_ids" { type = map(string) }
variable "channel_secret_ids" { type = map(string) }
variable "analytics_secret_ids" { type = map(string) }
variable "assets_bucket" { type = string }
variable "backups_bucket" { type = string }
variable "temporal_address" { type = string }
variable "temporal_namespace" { type = string }
variable "hub_origin" {
  type        = string
  description = "Public https origin of the approval hub UI (WebAuthn origin)."
}
variable "hub_rp_id" {
  type        = string
  description = "WebAuthn relying party id: the hub host or a parent domain."
}
variable "render_workers" {
  type        = number
  default     = 1
  description = "Instances in the rendering worker pool."
}

# One service account per component: least privilege and separate trust boundaries.
resource "google_service_account" "svc" {
  for_each     = toset(["approval-hub", "gateway", "worker", "ops", "scheduler"])
  project      = var.project_id
  account_id   = "${each.value}-${var.env}"
  display_name = "${each.value} (${var.env})"
}

# The approval signing key lives in Cloud KMS (Ed25519) and never leaves it: only the approval
# hub's service account may ask KMS to sign. Everyone else gets the PUBLIC key as a secret.
resource "google_kms_key_ring" "approval" {
  project  = var.project_id
  name     = "approval-${var.env}"
  location = var.region
}

resource "google_kms_crypto_key" "approval_signing" {
  name     = "approval-signing"
  key_ring = google_kms_key_ring.approval.id
  purpose  = "ASYMMETRIC_SIGN"
  version_template {
    algorithm        = "EC_SIGN_ED25519"
    protection_level = "SOFTWARE"
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key_iam_member" "hub_signs" {
  crypto_key_id = google_kms_crypto_key.approval_signing.id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.svc["approval-hub"].email}"
}

resource "google_secret_manager_secret_iam_member" "hub_slack_webhook" {
  secret_id = var.platform_secret_ids["slack-webhook"]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["approval-hub"].email}"
}

# The gateway reads only the PUBLIC verification key and YouTube OAuth material.
resource "google_secret_manager_secret_iam_member" "gateway_public_key" {
  secret_id = var.platform_secret_ids["approval-public-key"]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["gateway"].email}"
}

resource "google_secret_manager_secret_iam_member" "gateway_oauth_client" {
  secret_id = var.platform_secret_ids["youtube-oauth-client"]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["gateway"].email}"
}

resource "google_secret_manager_secret_iam_member" "gateway_channel_tokens" {
  for_each  = var.channel_secret_ids
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["gateway"].email}"
}

resource "google_secret_manager_secret_iam_member" "worker_llm_key" {
  secret_id = var.platform_secret_ids["anthropic-api-key"]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["worker"].email}"
}

locals {
  common_env = {
    ENV                        = var.env
    YT_SECRETS_BACKEND         = "cloud"
    YT_SECRETS_CLOUD_PROJECT   = var.project_id
    YT_TEMPORAL_ADDRESS        = var.temporal_address
    YT_TEMPORAL_NAMESPACE      = var.temporal_namespace
    YT_STORAGE_BACKEND         = "object_store"
    YT_STORAGE_BUCKET          = var.assets_bucket
    YT_YOUTUBE_BACKEND         = "real" # only the gateway builds a client; others just validate
    YT_APPROVAL_PUBLIC_KEY_REF = "vault://approval-public-key"
  }

  # Each service connects with its own database login (its own role) and knows which one it is.
  db_secret = {
    "approval-hub" = "database-url-hub"
    "worker"       = "database-url-worker"
    "gateway"      = "database-url-gateway"
    "ops"          = "database-url-ops"
  }
  component = {
    "approval-hub" = "hub"
    "worker"       = "worker"
    "gateway"      = "gateway"
    "ops"          = "ops"
  }
  service_env = {
    for sa, secret in local.db_secret : sa => merge(local.common_env, {
      YT_DATABASE_URL_REF = "vault://${secret}"
      YT_COMPONENT        = local.component[sa]
    })
  }

  # The gateway is its own service: the only holder of YouTube credentials. Only the worker's
  # service account may call it (Cloud Run IAM) and its ID token is checked again in the app.
  gateway_audience = "gateway-${var.env}"
  gateway_env = {
    YT_GATEWAY_AUDIENCE        = local.gateway_audience
    YT_GATEWAY_ALLOWED_CALLERS = jsonencode([google_service_account.svc["worker"].email])
  }
  worker_env = {
    YT_GATEWAY_URL      = google_cloud_run_v2_service.svc["gateway"].uri
    YT_GATEWAY_AUDIENCE = local.gateway_audience
  }

  # Settings only the approval hub reads (the production validator refuses anything weaker).
  hub_env = {
    YT_HUB_ORIGIN              = var.hub_origin
    YT_HUB_RP_ID               = var.hub_rp_id
    YT_HUB_COOKIE_SECURE       = "true"
    YT_HUB_TRUST_FORWARDED_FOR = "true" # the proxy in front appends the client address last
    YT_HUB_KEY_BACKEND         = "kms"
    YT_HUB_KMS_KEY_RESOURCE    = "${google_kms_crypto_key.approval_signing.id}/cryptoKeyVersions/1"
    YT_HUB_SIGNALER_BACKEND    = "temporal"
    YT_HUB_TEMPORAL_ADDRESS    = var.temporal_address
    YT_HUB_TEMPORAL_NAMESPACE  = var.temporal_namespace
    YT_HUB_NOTIFIER_BACKEND    = "slack"
    YT_HUB_SLACK_WEBHOOK_REF   = "vault://slack-webhook"
  }
}

# Database URL: each service reads ONLY its own login.
resource "google_secret_manager_secret_iam_member" "database_url" {
  for_each  = local.db_secret
  secret_id = var.platform_secret_ids[each.value]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc[each.key].email}"
}

# Workers verify with the PUBLIC key only (the hub alone holds the signing key).
resource "google_secret_manager_secret_iam_member" "worker_public_key" {
  for_each  = toset(["worker", "ops"])
  secret_id = var.platform_secret_ids["approval-public-key"]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc[each.key].email}"
}

# Analytics tokens are readable by the ops jobs only, never by workers or the gateway.
resource "google_secret_manager_secret_iam_member" "ops_analytics_tokens" {
  for_each  = var.analytics_secret_ids
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["ops"].email}"
}

# Object storage: workers write renders, the gateway and hub only read; ops reads and writes
# backups. Objects are content-addressed, so nothing is ever overwritten with different bytes.
# Create and read only: the worker can neither delete nor overwrite an approved asset.
resource "google_storage_bucket_iam_member" "assets_worker" {
  for_each = toset(["roles/storage.objectCreator", "roles/storage.objectViewer"])
  bucket   = var.assets_bucket
  role     = each.value
  member   = "serviceAccount:${google_service_account.svc["worker"].email}"
}
resource "google_storage_bucket_iam_member" "assets_readers" {
  for_each = toset(["gateway", "approval-hub", "ops"])
  bucket   = var.assets_bucket
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${google_service_account.svc[each.key].email}"
}
resource "google_storage_bucket_iam_member" "backups_ops" {
  for_each = toset(["roles/storage.objectCreator", "roles/storage.objectViewer"])
  bucket   = var.backups_bucket
  role     = each.value
  member   = "serviceAccount:${google_service_account.svc["ops"].email}"
}

resource "google_cloud_run_v2_service" "svc" {
  # The worker is not a service: it is the pull-based worker pool below (no inbound traffic).
  for_each = {
    "approval-hub" = { image = var.images["approval_hub"], module = "ytcore.approvals.service.main:app" }
    "gateway"      = { image = var.images["gateway"], module = "ytcore.gateway.main:app" }
  }
  project  = var.project_id
  name     = "${each.key}-${var.env}"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  # The gateway's ID-token audience (callers fetch a token for exactly this audience).
  custom_audiences = each.key == "gateway" ? [local.gateway_audience] : []

  template {
    service_account = google_service_account.svc[each.key].email
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        subnetwork = var.subnet_id
      }
    }
    containers {
      image   = each.value.image
      command = ["python", "-m", "uvicorn", each.value.module, "--host", "0.0.0.0", "--port", "8080"]
      dynamic "env" {
        for_each = merge(
          local.service_env[each.key],
          each.key == "approval-hub" ? local.hub_env : {},
          each.key == "gateway" ? local.gateway_env : {},
        )
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
}

# Rendering worker pool: pull-based Temporal workers (no inbound traffic), scaled by hand.
resource "google_cloud_run_v2_worker_pool" "render" {
  project  = var.project_id
  name     = "render-worker-${var.env}"
  location = var.region

  scaling {
    manual_instance_count = var.render_workers
  }

  template {
    service_account = google_service_account.svc["worker"].email
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        subnetwork = var.subnet_id
      }
    }
    containers {
      image   = var.images["worker"]
      command = ["python", "-m", "ytcore.workflow.worker", "run"]
      resources {
        limits = {
          cpu    = "4"
          memory = "8Gi"
        }
      }
      dynamic "env" {
        for_each = merge(local.service_env["worker"], local.worker_env)
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
}

# Scheduled operational jobs (one container image, different commands). Every job is idempotent.
locals {
  jobs = {
    planner   = { command = ["python", "-m", "ytcore.workflow.worker", "plan-once"], schedule = "5 * * * *", timeout = "300s" }
    monitor   = { command = ["python", "-m", "ytcore.observability.monitor", "check"], schedule = "*/10 * * * *", timeout = "120s" }
    breaker   = { command = ["python", "-m", "ytcore.safety.breaker", "run-once"], schedule = "*/30 * * * *", timeout = "120s" }
    tiers     = { command = ["python", "-m", "ytcore.tiers", "run-once"], schedule = "*/15 * * * *", timeout = "120s" }
    analytics = { command = ["python", "-m", "ytcore.analytics.ingest", "run-daily"], schedule = "30 3 * * *", timeout = "1800s" }
    backup    = { command = ["python", "-m", "ytcore.ops.backup", "backup", "--dir", "/tmp/backup", "--bucket", var.backups_bucket], schedule = "15 2 * * *", timeout = "3600s" }
  }
}

resource "google_cloud_run_v2_job" "ops" {
  for_each = local.jobs
  project  = var.project_id
  name     = "${each.key}-${var.env}"
  location = var.region

  template {
    template {
      service_account = google_service_account.svc["ops"].email
      timeout         = each.value.timeout
      max_retries     = 1
      vpc_access {
        egress = "ALL_TRAFFIC"
        network_interfaces {
          subnetwork = var.subnet_id
        }
      }
      containers {
        image   = var.images["worker"]
        command = each.value.command
        dynamic "env" {
          for_each = local.service_env["ops"]
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_jobs" {
  for_each = google_cloud_run_v2_job.ops
  project  = var.project_id
  location = var.region
  name     = each.value.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.svc["scheduler"].email}"
}

resource "google_cloud_scheduler_job" "ops" {
  for_each  = local.jobs
  project   = var.project_id
  region    = var.region
  name      = "${each.key}-${var.env}"
  schedule  = each.value.schedule
  time_zone = "Etc/UTC"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.ops[each.key].name}:run"
    oauth_token {
      service_account_email = google_service_account.svc["scheduler"].email
    }
  }
}

# Metrics exporter (Prometheus text on /metrics), scraped by Managed Prometheus or Grafana Agent.
resource "google_cloud_run_v2_service" "exporter" {
  project  = var.project_id
  name     = "metrics-exporter-${var.env}"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.svc["ops"].email
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        subnetwork = var.subnet_id
      }
    }
    containers {
      image   = var.images["worker"]
      command = ["python", "-m", "ytcore.observability.exporter", "--port", "8080"]
      dynamic "env" {
        for_each = local.service_env["ops"]
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
}

output "job_names" {
  value = [for j in google_cloud_run_v2_job.ops : j.name]
}

output "service_accounts" {
  value = { for k, sa in google_service_account.svc : k => sa.email }
}

# Only the worker's service account may invoke the gateway (nobody else, and nothing public).
resource "google_cloud_run_v2_service_iam_member" "worker_calls_gateway" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.svc["gateway"].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.svc["worker"].email}"
}

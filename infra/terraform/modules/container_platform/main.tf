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

# The approval hub alone can read the private signing key.
resource "google_secret_manager_secret_iam_member" "hub_signing_key" {
  secret_id = var.platform_secret_ids["approval-signing-key"]
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
    YT_DATABASE_URL_REF        = "vault://database-url"
    YT_APPROVAL_PUBLIC_KEY_REF = "vault://approval-public-key"
  }
}

# Database URL: every component that talks to Postgres.
resource "google_secret_manager_secret_iam_member" "database_url" {
  for_each  = toset(["approval-hub", "gateway", "worker", "ops"])
  secret_id = var.platform_secret_ids["database-url"]
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
resource "google_storage_bucket_iam_member" "assets_worker" {
  bucket = var.assets_bucket
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.svc["worker"].email}"
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
  for_each = {
    "approval-hub" = var.images["approval_hub"]
    "gateway"      = var.images["gateway"]
    "worker"       = var.images["worker"]
  }
  project  = var.project_id
  name     = "${each.key}-${var.env}"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.svc[each.key].email
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        subnetwork = var.subnet_id
      }
    }
    containers {
      image = each.value
      dynamic "env" {
        for_each = local.common_env
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
        for_each = local.common_env
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
      service_account = google_service_account.svc[each.key == "planner" ? "worker" : "ops"].email
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
          for_each = local.common_env
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
        for_each = local.common_env
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

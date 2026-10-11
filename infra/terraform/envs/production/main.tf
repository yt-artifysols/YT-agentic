terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
  # Remote state is configured at init time: terraform init -backend-config=...
  backend "gcs" {}
}

variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "temporal_address" {
  type        = string
  description = "Temporal frontend (Temporal Cloud or self-hosted), e.g. <ns>.<acct>.tmprl.cloud:7233."
}
variable "alert_email" { type = string }
variable "render_workers" {
  type    = number
  default = 2
}
variable "channel_ids" {
  type    = list(string)
  default = []
}
variable "images" {
  type        = map(string)
  description = "Pinned image digests; required in production (no placeholder default)."
}

locals { env = "production" }

provider "google" {
  project = var.project_id
  region  = var.region
}

module "network" {
  source     = "../../modules/network"
  project_id = var.project_id
  region     = var.region
  name       = "ytcore-${local.env}"
}

module "postgres" {
  source                      = "../../modules/postgres"
  project_id                  = var.project_id
  region                      = var.region
  name                        = "ytcore-${local.env}"
  network_id                  = module.network.network_id
  depends_on_private_services = module.network.private_services_connection
  deletion_protection         = true
}

module "object_storage" {
  source      = "../../modules/object_storage"
  project_id  = var.project_id
  location    = var.region
  bucket_name = "${var.project_id}-ytcore-assets-${local.env}"
}

module "backups" {
  source      = "../../modules/backups"
  project_id  = var.project_id
  bucket_name = "${var.project_id}-ytcore-backups-${local.env}"
}

module "secrets" {
  source      = "../../modules/secrets_manager"
  project_id  = var.project_id
  channel_ids = var.channel_ids
}

module "platform" {
  source               = "../../modules/container_platform"
  project_id           = var.project_id
  region               = var.region
  env                  = local.env
  subnet_id            = module.network.subnet_id
  images               = var.images
  platform_secret_ids  = module.secrets.platform_secret_ids
  channel_secret_ids   = module.secrets.channel_secret_ids
  analytics_secret_ids = module.secrets.analytics_secret_ids
  assets_bucket        = module.object_storage.bucket_name
  backups_bucket       = module.backups.bucket_name
  temporal_address     = var.temporal_address
  temporal_namespace   = "ytcore-${local.env == "production" ? "prod" : "staging"}"
  render_workers       = var.render_workers
}

module "monitoring" {
  source      = "../../modules/monitoring"
  project_id  = var.project_id
  env         = local.env
  alert_email = var.alert_email
  job_names   = module.platform.job_names
}

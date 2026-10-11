terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

variable "project_id" { type = string }
variable "region" { type = string }
variable "name" { type = string }
variable "network_id" { type = string }
variable "tier" {
  type    = string
  default = "db-custom-2-7680"
}
variable "deletion_protection" {
  type    = bool
  default = true
}
variable "depends_on_private_services" {
  type        = string
  description = "Private services connection id; forces ordering after VPC peering."
}

# Postgres 16 with pgvector (enabled per database via CREATE EXTENSION vector).
resource "google_sql_database_instance" "this" {
  project             = var.project_id
  name                = var.name
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.deletion_protection

  settings {
    tier              = var.tier
    availability_type = "REGIONAL"
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled    = false
      private_network = var.network_id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
    database_flags {
      name  = "log_min_duration_statement"
      value = "1000"
    }
  }

  lifecycle {
    precondition {
      condition     = var.depends_on_private_services != ""
      error_message = "private services connection is required"
    }
  }
}

resource "google_sql_database" "ytcore" {
  project  = var.project_id
  name     = "ytcore"
  instance = google_sql_database_instance.this.name
}

output "instance_connection_name" { value = google_sql_database_instance.this.connection_name }
output "private_ip" { value = google_sql_database_instance.this.private_ip_address }

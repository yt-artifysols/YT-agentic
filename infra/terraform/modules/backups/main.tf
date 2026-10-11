terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

variable "project_id" { type = string }
variable "bucket_name" { type = string }
variable "location" {
  type        = string
  default     = "US"
  description = "Multi-region or a region different from the primary, so one regional outage cannot take both."
}
variable "retention_days" {
  type    = number
  default = 30
}

# Logical Postgres dumps and the storage manifest. Versioned, never public, with a retention
# policy so a compromised job cannot delete recent backups. Lock the policy manually once the
# retention period is agreed (irreversible): see docs/runbooks/backup-restore.md.
resource "google_storage_bucket" "backups" {
  project                     = var.project_id
  name                        = var.bucket_name
  location                    = var.location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  retention_policy {
    retention_period = var.retention_days * 86400
    is_locked        = false
  }

  soft_delete_policy {
    retention_duration_seconds = 7 * 86400
  }

  lifecycle_rule {
    condition {
      age = var.retention_days * 3
    }
    action {
      type = "Delete"
    }
  }
}

output "bucket_name" { value = google_storage_bucket.backups.name }

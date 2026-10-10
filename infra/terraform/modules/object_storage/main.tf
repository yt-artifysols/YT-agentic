terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

variable "project_id" { type = string }
variable "location" { type = string }
variable "bucket_name" { type = string }
variable "noncurrent_version_retention_days" {
  type    = number
  default = 30
}

# Asset storage. Each channel writes only under its own prefix (channels/<channel_id>/);
# per-channel IAM conditions are applied by the container_platform module.
resource "google_storage_bucket" "assets" {
  project                     = var.project_id
  name                        = var.bucket_name
  location                    = var.location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = var.noncurrent_version_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

output "bucket_name" { value = google_storage_bucket.assets.name }

terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

variable "project_id" { type = string }
variable "env" { type = string }
variable "alert_email" {
  type        = string
  description = "Where infrastructure alerts go. Application alerts (stuck jobs, budgets, gateway failures) come from the ops monitor."
}
variable "job_names" {
  type        = list(string)
  description = "Scheduled Cloud Run jobs that must keep succeeding."
}

resource "google_monitoring_notification_channel" "email" {
  project      = var.project_id
  display_name = "ytcore ${var.env} alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

# A scheduled job failing (planner not planning, backups not running) is page-worthy.
resource "google_monitoring_alert_policy" "job_failed" {
  for_each     = toset(var.job_names)
  project      = var.project_id
  display_name = "ytcore ${var.env}: job ${each.value} failed"
  combiner     = "OR"

  conditions {
    display_name = "failed executions"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_job\" AND resource.labels.job_name = \"${each.value}\" AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result = \"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]
}

# The database running out of disk or CPU.
resource "google_monitoring_alert_policy" "database" {
  project      = var.project_id
  display_name = "ytcore ${var.env}: database disk or CPU high"
  combiner     = "OR"

  conditions {
    display_name = "disk utilisation above 80%"
    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND metric.type = \"cloudsql.googleapis.com/database/disk/utilization\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }
  conditions {
    display_name = "CPU above 85%"
    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND metric.type = \"cloudsql.googleapis.com/database/cpu/utilization\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.85
      duration        = "600s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]
}

# Gateway or approval hub returning 5xx.
resource "google_monitoring_alert_policy" "service_errors" {
  project      = var.project_id
  display_name = "ytcore ${var.env}: gateway or approval hub 5xx"
  combiner     = "OR"

  conditions {
    display_name = "5xx responses"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = monitoring.regex.full_match(\"(gateway|approval-hub)-${var.env}\") AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]
}

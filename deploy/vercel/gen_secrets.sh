#!/usr/bin/env bash
# Generates the staging secrets locally (nothing is sent anywhere) into ./.secrets-staging/ (0700):
#   approval.pem   private signing key  -> hub-api project only (YT_HUB_SIGNING_KEY_PEM)
#   approval.pub   public key           -> jobs project (APPROVAL_PUBLIC_KEY)
#   cron_secret    shared bearer secret -> BOTH projects (CRON_SECRET)
# Paste each into the Vercel dashboard (mark Sensitive), then `shred -u` the folder.
set -euo pipefail
umask 077
dir=".secrets-staging"
mkdir -p "$dir"
[ -e "$dir/approval.pem" ] && { echo "$dir already exists; refusing to overwrite keys" >&2; exit 1; }
openssl genpkey -algorithm ed25519 -out "$dir/approval.pem"
openssl pkey -in "$dir/approval.pem" -pubout -out "$dir/approval.pub"
openssl rand -base64 48 | tr -d '\n=+/' > "$dir/cron_secret"
echo "wrote $dir/ (approval.pem, approval.pub, cron_secret). Never commit it."

# Key rotation

## Approval signing key (Ed25519; hub private key, everyone else public key)
Rotate on a schedule (at least yearly) and **immediately** if the hub's key store or a hub
operator's access may have leaked. Tokens live at most 72 hours, so a rotation needs a short
overlap.

1. Pause publishing globally ([kill-switch.md](kill-switch.md)) if compromise is suspected;
   otherwise schedule a quiet window.
2. Production: create a NEW CryptoKeyVersion of the `approval-signing` key in Cloud KMS
   (the private key never leaves KMS) and point `YT_HUB_KMS_KEY_RESOURCE` at it (Terraform
   `container_platform`, `.../cryptoKeyVersions/<n>`). Dev only: `python -m
   ytcore.approvals.service.cli gen-dev-key`.
3. Publish the new **public** key (`gcloud kms keys versions get-public-key <n> ...`) to the
   `approval-public-key` secret (a new version). Workers
   and the gateway read `latest` at start: restart them (`render-worker`, `gateway`).
4. Restart the hub so it signs with the new key.
5. Tokens signed by the old key stop verifying: any job in `scheduled`/`publishing` needs a fresh
   approval (re-approve in the hub). Jobs in `awaiting_approval` are unaffected.
6. Disable the old key version. Verify with one test approval -> publish on a private test video.

Known gap: tokens carry no key id, so there is no overlap period where both keys verify.
Plan rotations for when nothing is scheduled.

## Channel OAuth refresh token (YouTube upload; gateway only)
Rotate when a channel owner re-links, a token is revoked, or on suspicion of leak.
1. Pause the channel.
2. The channel owner re-runs the OAuth consent in a browser (outside this repo); put the new
   refresh token into Secret Manager as a **new version** of `youtube-refresh-token-<channel>`
   (value entered by the operator in the console, never in a ticket or chat).
3. Revoke the old grant in the owner's Google account security page.
4. Resume the channel; check the next upload (private) succeeds.

## Analytics token (read-only; ops jobs only)
Same as above for `youtube-analytics-token-<channel>`; no pause needed.

## LLM and other API keys
New version in Secret Manager, restart workers, revoke the old key at the provider.

## Never
Paste a key into code, a prompt, a log, a ticket or this repo. The log scrubber and gitleaks
are a net, not permission.

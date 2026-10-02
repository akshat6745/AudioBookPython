# Deploying to a Google Cloud e2-micro (Always Free)

One small VM runs the API behind Caddy (automatic HTTPS). The VM holds no
data — D1 and R2 do — so it can be recreated at any time without loss.

## Free-tier guardrails

`scripts/gce/create-vm.sh` sets all of these; don't change them:

| Setting | Value | Why |
|---|---|---|
| Machine type | `e2-micro` | Only free machine type |
| Zone | `us-west1-c` | Free only in us-west1 / us-central1 / us-east1; us-west1 is closest to Asia |
| Boot disk | 30 GB `pd-standard` | Free allowance is 30 GB *standard* PD — the default "balanced" disk is billed |
| Network tier | `STANDARD` | 200 GB/month free egress (Premium gets ~1 GB) |

Only ever run **one** e2-micro in the billing account — the allowance is one
VM's worth of hours per month.

## First-time setup

1. Log in and select your project:
   ```bash
   scripts/gce/login.sh              # logs in, lists your projects
   scripts/gce/login.sh PROJECT_ID   # selects one
   ```
   No project yet? Create one at console.cloud.google.com/projectcreate and
   link a billing account to it (required even for free-tier usage).
   All `scripts/gce/` scripts use an **isolated gcloud config** at
   `~/.config/gcloud-audiobook` (and their own SSH key inside it), so this
   login never changes your default `gcloud` account or project. To run other
   gcloud commands against this deployment, prefix them:
   `CLOUDSDK_CONFIG=~/.config/gcloud-audiobook gcloud ...`
2. **Budget alert** (catches any billable mistake within a day): Console →
   Billing → Budgets & alerts → Create budget → amount `$1`, alerts at 50/100%.
3. Create the VM:
   ```bash
   scripts/gce/create-vm.sh
   ```
4. Make sure the repo-root `.env` has **production** values (see
   `app/core/settings.py`; `SUPABASE_URL`/`SUPABASE_KEY` are still required).
   `ENVIRONMENT`, `DEBUG` and `PORT` are pinned by `docker-compose.prod.yml`.
5. Deploy:
   ```bash
   scripts/gce/deploy.sh
   ```
   The API is served at `https://<VM-IP>.sslip.io` until you set up a domain.
6. The next day, check Billing → Reports shows **$0.00** (in particular that the
   VM's external IPv4 address isn't being charged).

## Deploying updates

Commit, then run `scripts/gce/deploy.sh`. It ships `git archive HEAD`, so
**uncommitted changes are not deployed**, and only tracked files reach the
server. `.env` is copied separately on every deploy.

## Using your own domain

Point an `A` record at the VM's IP, then:
```bash
DOMAIN=api.example.com scripts/gce/deploy.sh
```
Pass the same `DOMAIN` on every deploy.

## Notes

- **Don't stop the VM** — stopping releases the ephemeral IP (reboots keep it).
  If the IP changes, the sslip.io URL changes with it.
- Logs: `CLOUDSDK_CONFIG=~/.config/gcloud-audiobook gcloud compute ssh audiobook-api --zone=us-west1-c --ssh-key-file=~/.config/gcloud-audiobook/ssh/google_compute_engine --command='cd ~/release && sudo docker compose -f docker-compose.prod.yml logs --tail=100'`
- To remove this login entirely: `rm -rf ~/.config/gcloud-audiobook`
- After switching the app to the new URL, retire Render and the
  `.github/workflows/keep-alive.yml` workflow (it only exists to keep Render warm).

# 📘 Google Workspace → GitHub EMU Synchronization

This utility synchronizes users from **Google Workspace (Admin SDK)** into **GitHub Enterprise Managed Users (EMU)** using the **SCIM API**.

It supports:
- Owner + Member group merging
- Role-based member classification
- Create / Update / Suspend operations
- Dry-run mode
- Retry with exponential backoff
- Console logging and CSV reporting
- GitHub Actions summary output

## What this script does

1. Reads group membership from two Google Workspace groups:
   - `OWNER_GROUP`
   - `MEMBER_GROUP`
2. Loads each Google Workspace user profile for all found emails.
3. Normalizes the user record for GitHub SCIM, including:
   - `externalId`
   - `userName` / email
   - `displayName`
   - `givenName`
   - `familyName`
   - `active` status based on Google suspension
   - `role` as `enterprise_owner` or `enterprise_member`
4. Reads all GitHub SCIM users for the enterprise.
5. Matches GitHub users first by `externalId` and then by email.
6. Creates GitHub users for new Google Workspace members.
7. Updates existing users when role or active status changes.
8. Suspends GitHub users that are no longer in either Google group.

## Why logging is important

The script logs every major step and reports SCIM response statuses. That helps determine whether errors are coming from:
- Google Workspace API authentication and service account setup
- GitHub SCIM authentication, token permissions, or request payloads

It also writes a CSV report at `reports/sync-report.csv` with:
- `email`
- `action`
- `status`
- `result`

---

## Prerequisites

### Environment variables
The script relies on these environment variables:
- `GITHUB_ENTERPRISE` — GitHub Enterprise slug
- `GITHUB_TOKEN` — SCIM-enabled GitHub token with EMU provisioning permissions
- `GOOGLE_ADMIN_USER` — Google Workspace admin email to impersonate
- `OWNER_GROUP` — Google Workspace owner group email
- `MEMBER_GROUP` — Google Workspace member group email
- `DRY_RUN` — optional, set to `true` to perform a dry run
- `MAX_RETRIES` — optional, retry count for transient errors
- `RETRY_BACKOFF` — optional, exponential backoff base
- `GITHUB_STEP_SUMMARY` — optional, GitHub Actions summary output path

### Google Workspace setup
- Enable the Admin SDK API in Google Cloud.
- Create a service account with Domain-Wide Delegation.
- Grant the service account access to impersonate the admin user.
- Ensure the service account JSON is available as `service-account.json` in the repo root.
- Required scopes:
  - `https://www.googleapis.com/auth/admin.directory.user.readonly`
  - `https://www.googleapis.com/auth/admin.directory.group.member.readonly`

### GitHub setup
- Enable SCIM provisioning for the enterprise.
- Generate a GitHub token with SCIM and enterprise user management permissions.
- If you see `401` from GitHub, the token is invalid, expired, or missing SCIM permissions.

---

## Execution

From the repository root:

```bash
python3 sync-users.py
```

For a dry run without applying changes:

```bash
DRY_RUN=true python3 sync-users.py
```

### Output
- Console logging for each sync step
- `reports/sync-report.csv` with action history
- GitHub Actions summary if `GITHUB_STEP_SUMMARY` is set

---

## Typical behavior

- New Google users are created in GitHub EMU.
- Existing GitHub users are matched by `externalId` or email.
- Role changes are applied when a user moves between owner/member groups.
- Suspended users in Google are marked inactive in GitHub.
- Users removed from both groups are suspended in GitHub.

---

## Troubleshooting

### 401 Unauthorized

A `401` failure can happen on either side:
- GitHub SCIM side: invalid or insufficient `GITHUB_TOKEN`, wrong enterprise slug, or missing SCIM permissions.
- Google Workspace side: invalid service account JSON, wrong impersonated admin email, or missing domain-wide delegation.

If you updated the token recently, verify:
- `GITHUB_TOKEN` is still valid
- it has GitHub SCIM provisioning permissions
- the enterprise slug in `GITHUB_ENTERPRISE` is correct

If the failure is from Google Workspace, verify:
- `service-account.json` exists and is readable
- `GOOGLE_ADMIN_USER` is a super admin account
- domain-wide delegation is configured for the service account
- Admin SDK API is enabled

### Verifying which system failed

- Check console logs for the first failing request.
- `401` with GitHub SCIM endpoint means GitHub token/auth failure.
- `401` while calling the Google Admin API means Google auth or service account issue.
- The script now logs errors and writes `ERROR` into the CSV for failed actions.

---

## Project structure

- `sync-users.py` — main sync script
- `service-account.json` — Google service account credentials (required)
- `reports/sync-report.csv` — generated sync report

---

## Notes

- The script is idempotent when run against the same Google group membership.
- If a user was suspended previously and later re-added to a Google group, it will be restored on the next run.



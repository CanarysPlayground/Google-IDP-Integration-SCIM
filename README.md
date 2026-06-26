# 📘 Google Workspace → GitHub EMU Synchronization

This utility synchronizes users from **Google Workspace (Admin SDK)** into **GitHub Enterprise Managed Users (EMU)** using the **SCIM API**.

It supports:
- Owner + Member group merging
- Role-based user classification
- Create / Update / Suspend operations
- Dry-run mode
- Retry with exponential backoff
- CSV + GitHub Actions summary reporting

---

# 🚀 Architecture

Google Workspace
   ├── Owner Group
   └── Member Group
        ↓ (merge + dedupe, owners take precedence)
Normalized User Set
        ↓
Google Admin API (profile fetch only for required users)
        ↓
GitHub SCIM API (EMU Users)
        ↓
Sync Engine
   ├── Create users
   ├── Update users
   ├── Suspend users
   └── Skip unchanged users
        ↓
Reports
   ├── CSV report (reports/sync-report.csv)
   └── GitHub Actions Summary

---

# ✨ Features

## 👤 Identity Management
- Reads Owner and Member Google Groups
- Merges users with owner precedence
- Fetches only required Google user profiles

## 🔁 Synchronization Logic
- CREATE → User exists in Google but not GitHub
- UPDATE → Role or active status changed
- SUSPEND → User missing in Google
- SKIP → No change required

## 🛡 Reliability
- Retry logic for 429 / 5xx errors
- Exponential backoff strategy
- Safe idempotent SCIM operations

## 🧪 Execution Modes
- Normal mode (live sync)
- Dry-run mode (no changes applied)

## 📊 Reporting
- CSV audit log
- GitHub Actions summary output

---

# 📦 Prerequisites

## GitHub Enterprise
- Enterprise Managed Users (EMU) enabled
- SCIM provisioning enabled
- SCIM-enabled GitHub token

## Google Workspace
- Admin SDK enabled
- Service account with Domain-Wide Delegation
- Access to:
  - Directory API (Users)
  - Directory API (Groups)

---

# 🔐 Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `GITHUB_ENTERPRISE` | GitHub Enterprise slug |
| `GITHUB_TOKEN` | SCIM-enabled token |
| `GOOGLE_ADMIN_USER` | Google Workspace admin email |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service account JSON |

---

# 📁 Project Structure

#!/usr/bin/env python3
"""
Google Workspace -> GitHub Enterprise Managed Users (EMU) SCIM sync.

Design:
  - PROVISIONING (create/update) always runs, every trigger, scheduled or manual.
  - DE-PROVISIONING (suspend) NEVER runs automatically. Users who are in
    GitHub but no longer in any configured Google Group are only FLAGGED in
    the report. Actual suspension only happens when this script is run with
    ENABLE_SUSPEND=true, which the workflow only sets on an explicit manual
    trigger with the `suspend` input checked.

This means a scheduled run can never suspend anyone, regardless of group
configuration mistakes, partial reads, or anything else -- the worst a bad
config can do on a scheduled run is fail to provision someone or mis-flag
someone for review.
"""

import csv
import json
import logging
import os
import sys
import time
from typing import Dict, List, Set

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger(__name__)

# =========================
# ENV
# =========================

GITHUB_ENTERPRISE = os.environ["GITHUB_ENTERPRISE"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

GOOGLE_ADMIN_USER = os.environ["GOOGLE_ADMIN_USER"]

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Master switch for de-provisioning. Only ever "true" on a deliberate manual
# run -- see sync-users.yml, which hardcodes this to false on schedule.
ENABLE_SUSPEND = os.getenv("ENABLE_SUSPEND", "false").lower() == "true"

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_BACKOFF = int(os.getenv("RETRY_BACKOFF", "2"))

# Extra safety net for the rare run where ENABLE_SUSPEND=true: if the number
# of users about to be suspended is an anomalously large fraction of all
# known GitHub users, refuse to apply and error out instead.
MAX_SUSPEND_RATIO = float(os.getenv("MAX_SUSPEND_RATIO", "0.2"))

REPORT_DIR = "reports"
REPORT_FILE = f"{REPORT_DIR}/sync-report.csv"

SCIM_BASE_URL = f"https://api.github.com/scim/v2/enterprises/{GITHUB_ENTERPRISE}/Users"

# =========================
# GROUP -> ROLE CONFIGURATION
# =========================

# Lower rank = higher-privilege role. Resolves a user who belongs to more
# than one configured group: the highest-privilege role wins.
ROLE_RANK = {
    "enterprise_owner": 0,
    "enterprise_member": 1,
}


def load_groups_config() -> Dict[str, str]:
    """
    Returns {google_group_email: github_role}.

    Any number of Google Groups can be onboarded, each mapped to a GitHub
    Enterprise role, via the GOOGLE_GROUPS_JSON env var, e.g.:

        GOOGLE_GROUPS_JSON='{
          "github-enterprise-owners@example.com": "enterprise_owner",
          "github-enterprise-members@example.com": "enterprise_member",
          "contractors@example.com": "enterprise_member"
        }'
    """
    raw = os.getenv("GOOGLE_GROUPS_JSON", "").strip()

    if not raw:
        raise ValueError(
            "GOOGLE_GROUPS_JSON is not set. Provide a JSON object mapping "
            "Google Group emails to GitHub roles, e.g. "
            '\'{"group@example.com": "enterprise_member"}\'.'
        )

    try:
        config = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"GOOGLE_GROUPS_JSON is not valid JSON: {e}")

    if not isinstance(config, dict) or not config:
        raise ValueError(
            "GOOGLE_GROUPS_JSON must be a non-empty JSON object of "
            "{group_email: role}"
        )

    invalid_roles = {r for r in config.values() if r not in ROLE_RANK}
    if invalid_roles:
        raise ValueError(
            f"Unsupported role(s) in GOOGLE_GROUPS_JSON: {invalid_roles}. "
            f"Supported roles: {list(ROLE_RANK)}"
        )

    return {group.strip().lower(): role for group, role in config.items()}

# =========================
# HEADERS
# =========================

def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/scim+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

# =========================
# RETRY WRAPPER
# =========================

def request_with_retry(method, url, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, timeout=60, **kwargs)

            if resp.status_code < 400:
                return resp

            if resp.status_code in [429, 500, 502, 503, 504]:
                raise Exception(f"Retryable: {resp.status_code}")

            return resp

        except Exception as e:
            wait = RETRY_BACKOFF ** attempt
            logger.warning(f"Retry {attempt}/{MAX_RETRIES} after error: {e}")
            time.sleep(wait)

    raise Exception(f"Failed after {MAX_RETRIES} retries: {url}")

# =========================
# GOOGLE AUTH
# =========================

def get_google_service():
    credentials = service_account.Credentials.from_service_account_file(
        "service-account.json",
        scopes=[
            "https://www.googleapis.com/auth/admin.directory.user.readonly",
            "https://www.googleapis.com/auth/admin.directory.group.member.readonly"
        ]
    ).with_subject(GOOGLE_ADMIN_USER)

    return build("admin", "directory_v1", credentials=credentials, cache_discovery=False)

# =========================
# GROUP MEMBERS
# =========================

def get_group_members(service, group_email: str) -> Set[str]:
    members = set()
    page_token = None

    while True:
        resp = service.members().list(groupKey=group_email, pageToken=page_token).execute()

        for m in resp.get("members", []):
            if "email" in m:
                members.add(m["email"].lower())

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return members

# =========================
# GOOGLE USERS (ONLY REQUIRED ONES)
# =========================

def get_google_users(service, emails: Set[str]):
    users = {}

    for email in emails:
        try:
            u = service.users().get(userKey=email).execute()
            users[email.lower()] = u
        except Exception as e:
            logger.warning(f"Skipping Google user {email}: {e}")

    return users

# =========================
# NORMALIZE
# =========================

def normalize(user, role: str):
    email = user["primaryEmail"].lower()

    return {
        "externalId": user["id"],
        "email": email,
        "userName": email,
        "displayName": user.get("name", {}).get("fullName", ""),
        "givenName": user.get("name", {}).get("givenName", ""),
        "familyName": user.get("name", {}).get("familyName", ""),
        "active": not user.get("suspended", False),
        "role": role
    }

# =========================
# GITHUB USERS
# =========================

def get_github_users() -> List[dict]:
    """
    Returns EVERY GitHub EMU SCIM user, including accounts with no
    externalId set (manual invites, JIT provisioning, etc.), so nothing is
    invisible to matching, flagging, or (when enabled) suspension.
    """
    users = []
    start_index = 1

    while True:
        resp = request_with_retry(
            "GET",
            SCIM_BASE_URL,
            headers=github_headers(),
            params={"startIndex": start_index, "count": 100}
        ).json()

        resources = resp.get("Resources", [])
        users.extend(resources)

        if len(resources) < 100:
            break

        start_index += 100

    return users

# =========================
# SCIM OPS
# =========================

def create_user(user):
    if DRY_RUN:
        return {"status_code": 200, "text": "DRY_RUN"}

    payload = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "externalId": user["externalId"],
        "active": user["active"],
        "userName": user["userName"],
        "displayName": user["displayName"],
        "name": {
            "givenName": user["givenName"],
            "familyName": user["familyName"]
        },
        "emails": [{"value": user["email"], "primary": True}],
        "roles": [{"value": user["role"]}]
    }

    return request_with_retry("POST", SCIM_BASE_URL, headers=github_headers(), json=payload)


def patch_user(user_id, operations):
    if DRY_RUN:
        return {"status_code": 200, "text": "DRY_RUN"}

    return request_with_retry(
        "PATCH",
        f"{SCIM_BASE_URL}/{user_id}",
        headers=github_headers(),
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": operations
        }
    )

# =========================
# REPORTING
# =========================

def init_report():
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(REPORT_FILE, "w") as f:
        csv.writer(f).writerow(["email", "action", "status", "result"])

def write_report(row):
    with open(REPORT_FILE, "a") as f:
        csv.writer(f).writerow(row)

# =========================
# MAIN ENGINE
# =========================

def sync():
    service = get_google_service()

    groups_config = load_groups_config()
    logger.info(f"Configured groups ({len(groups_config)}): {list(groups_config.keys())}")
    logger.info(f"ENABLE_SUSPEND = {ENABLE_SUSPEND}")

    # Resolve each user's role across however many groups they belong to.
    # Highest-privilege role wins if they're in more than one group.
    email_roles: Dict[str, str] = {}

    for group_email, role in groups_config.items():
        # Deliberately not caught: a failed group read must not silently
        # shrink the provisioning universe. Fail the run instead.
        members = get_group_members(service, group_email)
        logger.info(f"Group {group_email} -> role '{role}': {len(members)} member(s)")

        for email in members:
            current_role = email_roles.get(email)
            if current_role is None or ROLE_RANK[role] < ROLE_RANK[current_role]:
                email_roles[email] = role

    all_emails = set(email_roles.keys())
    logger.info(f"Total unique users across {len(groups_config)} group(s): {len(all_emails)}")

    google_raw = get_google_users(service, all_emails)
    google_users = {
        email: normalize(u, email_roles[email])
        for email, u in google_raw.items()
    }

    github_users = get_github_users()

    by_ext = {u["externalId"]: u for u in github_users if u.get("externalId")}
    by_email = {u.get("userName", "").lower(): u for u in github_users if u.get("userName")}

    stats = {"create": 0, "update": 0, "suspend": 0, "flagged": 0, "skip": 0, "error": 0}
    matched_github_ids = set()

    init_report()

    def report_action(email, action, resp):
        status = getattr(resp, "status_code", 200)
        result = "OK" if status < 400 else "ERROR"
        write_report([email, action, status, result])
        if status >= 400:
            stats["error"] += 1
            logger.error(f"{action} failed for {email}: {status} {getattr(resp, 'text', '')}")

    # ================= PROVISION: CREATE / UPDATE =================
    # This always runs, on every trigger.
    for email, g in google_users.items():

        gh = by_ext.get(g["externalId"]) or by_email.get(email)
        if gh and gh.get("externalId") != g["externalId"]:
            logger.warning(
                f"GitHub user {email} exists with externalId {gh.get('externalId')} "
                f"but Google externalId is {g['externalId']}. Using existing GitHub account."
            )

        if not gh:
            resp = create_user(g)
            stats["create"] += 1
            report_action(email, "CREATE", resp)
            continue

        matched_github_ids.add(gh["id"])

        needs_update = (
            gh.get("active") != g["active"]
            or gh.get("roles", [{}])[0].get("value") != g["role"]
        )

        if needs_update:
            resp = patch_user(
                gh["id"],
                [
                    {"op": "replace", "path": "active", "value": g["active"]},
                    {"op": "replace", "path": "roles", "value": [{"value": g["role"]}]}
                ]
            )
            stats["update"] += 1
            report_action(email, "UPDATE", resp)
        else:
            stats["skip"] += 1

    # ================= STALE USERS: FLAG or SUSPEND =================
    # "Stale" = present in GitHub, not matched to anyone in the configured
    # Google Groups. By default we only ever FLAG these in the report.
    # Actual suspension requires ENABLE_SUSPEND=true, which only happens on
    # an explicit manual workflow run with the `suspend` input checked.
    stale_users = []
    for gh in github_users:
        if gh["id"] in matched_github_ids:
            continue

        user_email = gh.get("userName", "").lower()
        if user_email in google_users:
            continue

        stale_users.append(gh)

    if not ENABLE_SUSPEND:
        for gh in stale_users:
            user_email = gh.get("userName", "").lower()
            stats["flagged"] += 1
            write_report([
                user_email or gh["id"],
                "FLAGGED_FOR_SUSPENSION",
                "REVIEW",
                "Not found in any configured Google Group. Not suspended "
                "automatically -- run this workflow manually with suspend=true "
                "to de-provision."
            ])
        if stale_users:
            logger.info(
                f"{len(stale_users)} stale user(s) flagged for review, none suspended "
                f"(ENABLE_SUSPEND is false)."
            )
    else:
        total_known = len(github_users)
        suspend_ratio = (len(stale_users) / total_known) if total_known else 0

        if total_known > 0 and suspend_ratio > MAX_SUSPEND_RATIO:
            msg = (
                f"Refusing to suspend {len(stale_users)}/{total_known} users "
                f"({suspend_ratio:.0%}) -- exceeds MAX_SUSPEND_RATIO "
                f"({MAX_SUSPEND_RATIO:.0%}). No suspensions were applied. "
                f"Investigate group configuration, or re-run with a higher "
                f"MAX_SUSPEND_RATIO if this is genuinely expected."
            )
            logger.error(msg)
            stats["error"] += 1
            write_report(["__SAFETY_ABORT__", "SUSPEND_BLOCKED", 0, msg])
        else:
            for gh in stale_users:
                user_email = gh.get("userName", "").lower()
                resp = patch_user(gh["id"], [
                    {"op": "replace", "path": "active", "value": False}
                ])
                stats["suspend"] += 1
                report_action(user_email or gh["id"], "SUSPEND", resp)

    # ================= SUMMARY =================
    logger.info("SYNC SUMMARY")
    logger.info(json.dumps(stats, indent=2))

    with open(os.environ.get("GITHUB_STEP_SUMMARY", "/tmp/summary.md"), "a") as f:
        f.write("## Google -> GitHub Sync\n")
        f.write(json.dumps(stats, indent=2))

    return stats


if __name__ == "__main__":
    try:
        result = sync()
        if result["error"] > 0:
            sys.exit(1)
    except Exception as e:
        logger.error(str(e))
        sys.exit(1)

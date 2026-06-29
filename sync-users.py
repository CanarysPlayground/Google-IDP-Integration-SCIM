#!/usr/bin/env python3

import csv
import json
import logging
import os
import sys
import time
from typing import Dict, List, Tuple, Set

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
OWNER_GROUP = os.environ["OWNER_GROUP"]
MEMBER_GROUP = os.environ["MEMBER_GROUP"]

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_BACKOFF = int(os.getenv("RETRY_BACKOFF", "2"))

REPORT_DIR = "reports"
REPORT_FILE = f"{REPORT_DIR}/sync-report.csv"

SCIM_BASE_URL = f"https://api.github.com/scim/v2/enterprises/{GITHUB_ENTERPRISE}/Users"

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

def normalize(user, owners: Set[str]):
    email = user["primaryEmail"].lower()

    return {
        "externalId": user["id"],
        "email": email,
        "userName": email,
        "displayName": user.get("name", {}).get("fullName", ""),
        "givenName": user.get("name", {}).get("givenName", ""),
        "familyName": user.get("name", {}).get("familyName", ""),
        "active": not user.get("suspended", False),
        "role": "enterprise_owner" if email in owners else "enterprise_member"
    }

# =========================
# GITHUB USERS
# =========================

def get_github_users():
    users = {}
    start_index = 1

    while True:
        resp = request_with_retry(
            "GET",
            SCIM_BASE_URL,
            headers=github_headers(),
            params={"startIndex": start_index, "count": 100}
        ).json()

        resources = resp.get("Resources", [])

        for u in resources:
            if u.get("externalId"):
                users[u["externalId"]] = u

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

    owners = get_group_members(service, OWNER_GROUP)
    members = get_group_members(service, MEMBER_GROUP)

    all_emails = members | owners

    logger.info(f"Total unique users: {len(all_emails)}")

    google_raw = get_google_users(service, all_emails)

    google_users = {
        email: normalize(u, owners)
        for email, u in google_raw.items()
    }

    github_users = get_github_users()

    by_ext = {v["externalId"]: v for v in github_users.values()}
    by_email = {v.get("userName", "").lower(): v for v in github_users.values()}

    stats = {"create":0,"update":0,"suspend":0,"skip":0,"error":0}
    matched_github_ids = set()

    init_report()

    def report_action(email, action, resp):
        status = getattr(resp, "status_code", 200)
        result = "OK" if status < 400 else "ERROR"
        write_report([email, action, status, result])
        if status >= 400:
            stats["error"] += 1
            logger.error(f"{action} failed for {email}: {status} {getattr(resp, 'text', '')}")

    # ================= CREATE / UPDATE =================
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
                    {"op":"replace","path":"active","value":g["active"]},
                    {"op":"replace","path":"roles","value":[{"value":g["role"]}]}
                ]
            )
            stats["update"] += 1
            report_action(email, "UPDATE", resp)
        else:
            stats["skip"] += 1

    # ================= SUSPEND =================
    for ext_id, gh in by_ext.items():
        if gh["id"] in matched_github_ids:
            continue

        user_email = gh.get("userName", "").lower()
        if user_email in google_users:
            continue

        resp = patch_user(gh["id"], [
            {"op":"replace","path":"active","value":False}
        ])
        stats["suspend"] += 1
        report_action(user_email or gh["id"], "SUSPEND", resp)

    # ================= SUMMARY =================
    logger.info("SYNC SUMMARY")
    logger.info(json.dumps(stats, indent=2))

    with open(os.environ.get("GITHUB_STEP_SUMMARY", "/tmp/summary.md"), "a") as f:
        f.write("## Google → GitHub Sync\n")
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

# Google Workspace → GitHub Enterprise User Sync

## What is this, in plain terms?

Your company keeps track of "who works here and what team they're on" inside
**Google Workspace groups**. Separately, GitHub needs its own list of "who is
allowed to log in and what permission level they have."

Today, keeping those two lists in sync means someone manually creates GitHub
accounts, sets their role, and later remembers to remove access when someone
leaves. That's slow and easy to forget.

This project is a small automated tool that does that syncing for you. You
tell it which Google Groups matter and what GitHub role each group should
grant. When you run it:

- Anyone in those Google Groups who doesn't have GitHub access yet **gets an
  account created**.
- Anyone whose role or status changed in Google (e.g. promoted to owner,
  or suspended in Google) **gets updated** in GitHub to match.
- Anyone who has GitHub access but is **no longer in any of those Google
  Groups** gets **flagged** in a report for someone to review — it does
  **not** remove their access automatically. Removing access is a separate,
  deliberate step you choose to take (explained below).

Nothing runs on a timer. Nothing happens unless a person deliberately clicks
"Run workflow." That's intentional — it keeps a human in the loop for every
change, and it means the group list is always exactly what you see on screen
when you click run, never something hidden in a background schedule.

## Why does it flag instead of just removing access?

Because automatically removing someone's access is risky if the input data
is ever wrong (a typo in a group name, a Google API hiccup, someone editing
the wrong thing). This tool is built so that mistake, in the worst case,
means a departed employee's report/access. If suspension were automatic,
the same mistake could mean **locking out active employees by accident**,
which is a much worse failure. So:

- Granting/updating access → safe to automate, happens every run.
- Removing access → requires a human to explicitly say "yes, suspend these
  people" for that specific run.

## What you need before running this

Someone needs to have already set up these repository secrets (in GitHub
repo Settings → Secrets and variables → Actions):

| Secret | What it is |
|---|---|
| `GITHUB_ENTERPRISE` | Your GitHub Enterprise slug/name |
| `GITHUB_TOKEN` | A token with permission to manage SCIM users |
| `GOOGLE_ADMIN_USER` | A Google Workspace admin email the automation impersonates |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Credentials for a Google service account with directory read access |

If these aren't set up yet, this tool can't do anything — talk to whoever
manages your GitHub organization and Google Workspace admin console.

## How to run it — step by step

1. Go to the **Actions** tab in the repository.
2. Select the **"Google Workspace → GitHub EMU Synchronization"** workflow.
3. Click **"Run workflow"**. You'll see three options:

   | Input | What it means in plain terms |
   |---|---|
   | `groups_json` | The list of Google Groups to check, and what GitHub role each one grants. It's pre-filled with the currently known groups — you don't need to touch it unless you're adding a new group or testing something one-off. |
   | `suspend` | Leave this **unchecked** (default) if you just want to onboard new/changed people and get a report of who looks stale. Check it **only** if you've reviewed a previous report and are ready to actually remove access for those flagged people. |
   | `dry_run` | Check this to do a "practice run" — it goes through all the logic and tells you what it *would* do, without actually creating, updating, or suspending anything. Good for checking your `groups_json` is correct before a real run. |

4. Click the green **"Run workflow"** button.
5. Wait for it to finish, then open the run and download the **`sync-report`**
   artifact — a CSV file listing exactly what happened to every user.

## Understanding the report

Each row is one user and one action taken (or not taken) for them. Look at
the `action` column:

| Action you'll see | What it means |
|---|---|
| `CREATE` | A brand-new GitHub account was created for this person |
| `UPDATE` | Their existing GitHub account's role or active status was changed to match Google |
| `FLAGGED_FOR_SUSPENSION` | This person has GitHub access but isn't in any configured Google Group anymore. **Nothing was done to their account.** Review this list and decide whether they should lose access. |
| `SUSPEND` | This person's account was suspended (only appears if you ran with `suspend` checked) |
| `SUSPEND_BLOCKED` | A safety check refused to suspend anyone this run because an unusually large number of people looked stale — this almost always means the group list was wrong, not that lots of people actually left. Check your `groups_json` before trying again. |

## The two-step de-provisioning process, explained

Think of it as "propose, then approve":

1. **Run normally (suspend unchecked).** You get a report with anyone stale
   marked `FLAGGED_FOR_SUSPENSION`. Nobody's access changes yet.
2. **Review that list with a human eye.** Are these people who genuinely
   left, or does something look off (e.g. an entire team you forgot to add
   to `groups_json`)?
3. **If the list looks right, run again with `suspend` checked.** Now those
   specific stale users actually get suspended.

This means removing someone's GitHub access always takes two conscious
actions — see the report, then choose to act on it — never one automatic
step.

## Adding a new Google Group later

Open the workflow file (`.github/workflows/sync-users.yml`) and find the
`groups_json` input's `default` value. Add your new group as another
`"email": "role"` entry, keeping the existing ones exactly as they are:

```json
{
  "github-enterprise-owners@yourdomain.com": "enterprise_owner",
  "github-enterprise-members@yourdomain.com": "enterprise_member",
  "new-team@yourdomain.com": "enterprise_member"
}
```

Save that as a normal change to the file (a pull request, if your repo
requires one). The next time someone runs the workflow, that new group will
be included automatically — no code changes needed beyond this one line.

Valid roles are `enterprise_owner` and `enterprise_member`. If someone
happens to be in more than one of your configured groups at once, they get
whichever role is more powerful — being an owner anywhere always wins.

## Quick summary

- **Nothing happens on a schedule** — every run is a deliberate click.
- **Granting access is automatic** every time you run it.
- **Removing access is a deliberate two-step process**: see the flagged
  report, then explicitly re-run with suspension turned on.
- **A safety check** refuses to mass-suspend people if the numbers look
  abnormally high, to protect against a bad group list wiping out access
  for a whole team by mistake.

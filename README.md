# TMExpert

TMExpert is an Excel-first test management system. Its durable domain records live only in managed `.xlsx` workbooks under `application-data`; no database engine, ORM, or database file is used.

## Run

On Windows, double-click `Start-TMExpert.cmd`. It opens a **TMExpert Server**
window and then opens the app in your default browser. Leave the server window
open; it intentionally stays visible so startup errors are not hidden.

Or run it from PowerShell:

```powershell
python server.py
```

Open http://localhost:8787 and keep that PowerShell window open. The first run creates the required workbook layout and a small demo project.

### Sign-in and roles

Everyone signs in with an email and password. The first admin account is
`avery@tmexpert.local` with the temporary password `change-me`; TMExpert asks
for a new password at first sign-in. Admins add team members from
**Administration**. Each new member gets a temporary password and must choose
their own when they first sign in.

| Role | Can do |
|---|---|
| Admin | Everything: repository, approvals, cycles, runs, users, exports, audit history. |
| Team Member | See approved test cases, projects, plans and cycles; record execution status. |

The server checks permissions on every request; hiding buttons is only cosmetic.
Team members see only test cases whose approval status is **Approved**.

Passwords are stored as salted PBKDF2 hashes in `Users.xlsx`. Older unsalted
hashes still work once and are upgraded at the next sign-in. Passwords never
appear in API responses or the audit history. After 5 failed sign-ins an email
is locked for 5 minutes. Sessions end after 12 idle hours and when the server
restarts.

### Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `TMEXPERT_PORT` | `8787` | Port (the next free port is used if busy). |
| `TMEXPERT_HOST` | `127.0.0.1` | Set to `0.0.0.0` to let the team reach this computer over the network. |
| `TMEXPERT_DATA_DIR` | `application-data` | Use a different data folder (for example a test copy). |

## Storage guarantees

- A dedicated `ExcelStorageService` is the only component that reads or writes internal workbooks.
- Records use UUIDs, never worksheet row numbers.
- Per-workbook locks, a process-local write queue, temporary writes, validation, and atomic replacement protect writes.
- Every modification is audited with the signed-in user and the previous and new values. Audit, approval and version history are append-only.
- Workbooks are backed up before every bulk change, import, archive or schema upgrade, and at most every 5 minutes otherwise (30 copies kept per workbook).
- New columns are added in place (after a backup) when a newer version needs them, for example `case_key` in `Test_Cases.xlsx` and `must_change_password` in `Users.xlsx`.
- Every test case has a stable ID (the `case_key` column). When a title starts with an ID such as
  `SUR-TC-022 – Add question to survey`, that ID is used if it is free in the project; otherwise
  TMExpert assigns `<PROJECT KEY>-TC-0001`, `-0002`, and so on. Titles are never changed, and the ID stays
  the same if the title is edited later. Archived copies whose title ID is already taken keep a generated ID.
- Additive workbook schema changes are read without dropping extra columns on later writes. Unsupported layouts are reported with the missing and unexpected columns; the workbook is left untouched for recovery.
- Relationships, required fields, duplicate IDs, execution states, and project ownership are validated before a write.
- Read caching, filtered/paginated reads, batch writes, and a rebuildable in-memory search index avoid repeated workbook loads.
- Run one TMExpert server version per `application-data` directory. Stop an older server before starting an updated version so it cannot serve stale schema expectations.

## Layout

```
application-data/
  core/          Users.xlsx, Projects.xlsx
  repository/    Repository_Folders.xlsx, Test_Cases.xlsx, Test_Steps.xlsx, Test_Case_Versions.xlsx
  requirements/  Requirements.xlsx
  execution/     Test_Plans.xlsx, Test_Suites.xlsx, Test_Cycles.xlsx, Test_Runs.xlsx, Execution_Results.xlsx, Defects.xlsx
  history/       Approvals.xlsx, Audit_History.xlsx
  backups/       timestamped copies before writes
  indexes/       reserved for rebuilt indexes
  temp/          atomic working copies and lock files
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

The tests use temporary folders and never touch `application-data`.

## Test-case Excel workflow

The Excel format is unchanged. Open **Test Repository → ⚙** for:

- **Download Excel template** – the standard 14 columns: `Scenario Name`,
  `Expected Result`, `Folder Path`, `Objective`, `Preconditions`, `Priority`,
  `Test Type`, `Tags`, `Requirement Keys`, `Last Status`, `Step Number`, `Action`,
  `Step Expected Result`, `Test Data`.
- **Export Excel** – the same 14 columns with the current version's steps.
- **Export Excel with test case IDs** – the same, plus a `Test Case ID` column at the end.
- **Import Excel** – opens an **import preview**; nothing is written until you confirm.
- **Import history** – every import and rejected attempt, with the original file.

Rules (unchanged from earlier versions): `Scenario Name` is the only required
column; use one row per test step and repeat the scenario name or leave it blank on
continuation rows; step actions and step expected results go together; folder paths
must name a module in the project (a shorter, older path is accepted when it names
exactly one module); unknown requirement keys are created automatically. If any row
has a problem, nothing is imported.

The import preview:
- recognises the template, lists matched columns (aliases such as `Test Case`,
  `Module` or `Expected Outcome` are accepted) and columns it will ignore;
- uses the first sheet that has a `Scenario Name` column (you can pick another sheet);
- reports problems per row and column: missing scenario names, Excel error values
  (`#N/A`, `#REF!` …), titles over 200 characters, invalid priority or last status,
  incomplete or duplicated steps, unknown modules (with a "did you mean" suggestion),
  and test cases that already exist in the same module;
- shows every test case with its module, step count, priority and status.

Options (off by default): **Create modules that don't exist yet** (created as Draft),
and **Import duplicates anyway**. An optional `Test Case ID` column sets the ID of new
test cases (it must not already be used in the project).

On import, the repository workbooks are backed up first, the uploaded file is kept
in `application-data/imports/`, and the import is recorded in
`history/Import_History.xlsx` (who, when, file name and SHA-256, sheet, counts).

## Modules and approvals

Repository folders are **modules** (top level) and **submodules** (nested).
Test cases and modules both go through an approval lifecycle:

Draft → Pending Approval → Approved or Rejected (a reason is required)

- A rejected test case is edited (it returns to Draft) and submitted again.
- Editing an approved test case creates a new version and returns it to Draft
  until it is approved again. Cycles that already use the old version are not affected.
- Admins can also deprecate a test case and later restore it.
- Every step is recorded in `history/Approvals.xlsx` with who, when, which
  version and the comment. The stored status values (`Draft`, `In Review`,
  `Approved`, `Rejected`, `Deprecated`) are unchanged; `In Review` is shown as
  "Pending Approval".
- Team members see a test case only when it is Approved **and** its module and
  every parent module are Approved. Modules that existed before this version
  were marked Approved; new modules start as Draft.
- Saving a test case with the same title as another one in the same module asks
  for confirmation. Excel imports report such rows and offer "Import anyway".
- Bulk actions: submit, approve, reject, move, set priority, add/remove tags,
  deprecate and archive. Moving, priority and tag changes do not create a new version.
- Module paths are checked at startup and corrected if a module was moved
  without its path being updated. Excel files that still use the older, shorter
  path continue to import when it names exactly one module.

## Test cycles

Admins create cycles from **Test Execution → Create test cycle** in four steps:

1. **Cycle details:** name, testing type (Regression, Sanity, Smoke, Functional,
   Retesting, Integration, UAT, or a custom type), environment, build, start date,
   target completion date, description, and an optional test plan.
2. **Select test cases:** tick modules to include all their eligible test cases
   (submodules included), untick individual cases to exclude them, or find and add
   single cases from any module.
3. **Preview** exactly what will be added, grouped by module.
4. **Confirm** and create.

- Only Approved test cases in Approved modules are eligible. Deprecated, Rejected
  and archived cases are never added. Admins can explicitly include Draft and
  Pending cases.
- A test case can be in a cycle only once.
- **Snapshots:** each test case is added at its current version. The cycle keeps
  the ID, title, module path, priority and type as they were then, and the steps of
  that version are never changed. Later repository edits do not alter existing
  cycles or their results.
- More test cases can be added to an open cycle with the same selection screen.
  A test case that has not been executed can be removed again.
- Each cycle shows its completion, pass rate and status counts.

## Assigning work in a cycle

Open a cycle in **Test Execution**:

- **Assignments** tab: give each module a default owner. Every test case under
  that module (including submodules) goes to that person. A submodule's own
  default wins over its parent's. The team workload panel shows each person's
  open test cases across all active cycles.
- **Test cases** tab: select test cases and **Assign to…** someone (an individual
  assignment), or **Use module default** to remove an individual assignment.
  ▤ marks test cases that follow their module; ✎ marks individual assignments.
- **Board** tab: drag test cases between people (select several with Ctrl/⌘ and
  drag them together), or use each card's **Move to…** menu.
- **History** tab: every assignment change with who, when, from and to.

Rules:
- Individual assignments override the module default and are never changed when
  a module's owner changes.
- Changing a module's owner only moves test cases that are still Not Run.
  In-progress and executed test cases keep their owner unless reassigned explicitly.
- Reassigning never changes or removes execution results.
- If two people change the same assignment at once, the second change is refused
  and the board refreshes instead of silently overwriting.
- Only admins can assign. Team members record results only for test cases
  assigned to them.
- Module defaults are stored in `execution/Cycle_Assignments.xlsx`; every change is
  appended to `history/Assignment_History.xlsx`.

## Executing tests

**My Work** lists the test cases assigned to you in active cycles, open work first,
with your progress. Use the quick buttons (✓ Pass, ✗ Fail, ⦸ Blocked, – N/A) or
**Execute** for the full screen:

- the exact version's preconditions, expected result and steps;
- a result and actual result per step (**Mark all steps passed** for speed);
- the overall status, actual result, notes and defect IDs or links;
- evidence: paste a screenshot (Ctrl+V), drop or choose files (images, PDF,
  text/log/CSV/JSON/HAR, Excel, Word, ZIP, MP4/WebM; 10 MB each);
- **Save & next** moves to the next test case in your list.

Rules:
- Every save adds an entry to `execution/Execution_History.xlsx` (append-only):
  attempt number, status, previous status, actual result, notes, defects, evidence,
  step results, who and when. Nothing earlier is overwritten or lost.
- If someone else saved the same test case after you opened it, your save is
  refused with a prompt to reload, instead of overwriting their result.
- Results can be recorded by the assigned member (or an admin) while the cycle
  is not Completed. A cycle switches to In Progress with its first result.
- Evidence that is part of a saved result cannot be removed.
- Files are stored under `application-data/attachments/` and listed in
  `execution/Attachments.xlsx` with a SHA-256 checksum. A file whose content does
  not match its type, or that was altered on disk, is refused.
- Admins can switch My Work to everyone, a person, or unassigned work.
- Test cases also have an **Attachments** tab in the repository.

## Releases: plans, suites, defects and coverage

The full workflow is Repository → Test Suites → Test Plan → Cycles → Execution →
Defects → Retest → Readiness. The design and gap analysis are in
`docs/QA_PLATFORM_AUDIT.md`.

- **Test Plans** are the release or sprint. Each cycle belongs to one plan
  (`Test_Cycles.plan_id`). Existing cycles were linked to their plan automatically
  on first start, from the old hidden suite rows; nothing was deleted.
- A plan shows every cycle, its testers and modules, the **effective result** of
  each test case (the latest executed result across the plan's cycles), open
  defects by severity, and a **readiness verdict** (Ready / Not ready / Not
  started) checked against the plan's **exit criteria**: execution %, pass rate,
  open Critical / High defects, and blocked cases. Admins edit the criteria per
  plan. **Export Excel** downloads the plan report.
- **Test Suites** are reusable selections of modules and/or test cases. Build one
  with the same picker as a cycle (or **Save as suite** from the cycle wizard),
  then add it to any new cycle with **+ Add a saved suite…**. Test cases are never
  copied: each cycle keeps its own execution record, pinned to the case version.
- **Defects** (`Defects.xlsx`, keys like `LMS-DEF-0001`) can be logged straight
  from a failed or blocked execution, or linked to an existing one. A Jira key and
  link can be stored as the external reference. Free-text references typed in
  earlier executions are listed as *unregistered* and can be registered in one click.
  Marking a defect **Fixed** records the time; it then shows **Awaiting retest**
  until an affected case is executed again, then **Retest passed** or
  **Retest failed**, and can be **Verified** and **Closed**.
- **Retest** on a cycle (or a defect) creates a Retesting cycle in the same plan
  with the failed / blocked cases, keeping the original testers.
- **Requirements** shows coverage per requirement: Passing, Failing, Partly
  tested, Not run, or Not covered. Test cases link to requirements and store an
  automation status and reference; changing these does not create a new version.

## Comparing cycles

**Compare Cycles** shows how every test case behaved across testing cycles.

- Pick the cycles to include (by default the latest cycles that have results) and
  which two to compare as **previous** and **current**. Cycles are always shown in
  date order (start date, else creation date).
- Test cases are matched by their record, never by title, and each cell shows the
  version that cycle executed; **✎ changed** marks a different test case definition.
- Insights for previous → current: **Regressions** (passed → failed), **New
  failures**, **Repeated failures**, **Fixed** (failed → passed), **Consistently
  passing** (passed every time it ran in the selected cycles), **Blocked**,
  **Not executed**, **Newly added**, **Removed**, **Definition changed**, and
  **Reopened** (failed, later passed, then failed again). Click a card to filter.
- Filter by module, ID or title, insight, current status and current assignee.
- **Results by cycle** charts passed / blocked / failed / N/A / not executed per
  cycle with the pass rate; **Show as table** lists the same numbers.
- **Export Excel** downloads the matrix (a new export; existing templates are unchanged).

## Dashboard and reports

The **Dashboard** is computed from the workbooks each time it opens.

- Filters: cycle (default: all active cycles; or all cycles; or one cycle), module
  (includes submodules), member, and a date range. When no cycle is chosen, the
  date range selects cycles that **start** in the range; it also sets the window of
  the "executions per day" chart.
- Headline numbers: test cases (approved, pending approval), active and completed
  cycles, executed / total with completion %, pass rate (passed ÷ executed,
  excluding Not Applicable), passed, failed, blocked and not run.
- **Regression trend**: regressions, new failures and reopened failures for each
  recent cycle compared with the cycle before it (same rules as Compare Cycles).
- Module progress, team progress with open workload across active cycles,
  executions per day, and the latest failures with their defects.
- Team members see **My dashboard**, limited to their own test cases.

**Reports** (admins) downloads Excel files for the current dashboard filters:
execution report, module summary, team summary and defects report, plus links to
the cycle comparison and the repository export.

## Audit log and integrity

Every change is recorded in `history/Audit_History.xlsx` with the user, action
(Create, Update, Archive, Status change, Assign, Re-key …), the record, and its
previous and new values. Admins open **Audit Log** to filter by area, action, user,
date or text, see exactly which fields changed, and export to Excel.

Each new audit entry carries a SHA-256 checksum chained to the entry before it. The
Audit Log page re-checks the whole chain: if anyone edits, deletes or reorders
entries in the workbook outside TMExpert, the page names the first entry that no
longer matches. Entries written before this version are listed as "older entries".

## History files that only grow

Audit, approvals, test case versions, test steps, assignment, execution and import
history are append-only. When one of these workbooks reaches 10,000 rows it is
moved, unchanged, to a numbered part in a folder next to it (for example
`history/Audit_History_archive/Audit_History_0001.xlsx`) and a new, small workbook
continues. TMExpert reads all parts together; do not edit or delete the parts.
This keeps saving fast as history grows.

## Security

- Sign-in with salted PBKDF2 passwords; sessions end after 12 idle hours, 7 days at
  most, or when the server restarts. Five failed sign-ins lock an email for 5 minutes.
- Every API request is checked against the user's role on the server.
- Pages may only load scripts, styles and data from TMExpert itself
  (Content-Security-Policy), cannot be embedded in other sites, and record IDs from
  clients are restricted to safe characters.
- Uploaded files are allow-listed, content-checked, checksummed and served so they
  cannot run as web pages.
- CSV exports neutralise cells that spreadsheet apps would treat as formulas.
- Signing out clears everything the previous person had open in the browser tab.

## Sharing with the team on the office network

Double-click **`Share-TMExpert-On-Network.cmd`**. The server window prints a
**Team link** such as `http://192.168.1.20:8787` to send to colleagues on the same
office network or VPN. Keep the window open while people use TMExpert.

- It refuses to start while any active account still uses the default password
  `change-me`. Sign in with `Start-TMExpert.cmd` first and set real passwords.
- Windows must treat the office Wi-Fi/network as **Private**, and the first start
  shows a Windows Firewall prompt: allow Python on **Private** networks only.
- Plain HTTP is acceptable only on a trusted office network or VPN. For HTTPS, set
  `TMEXPERT_CERT` and `TMEXPERT_KEY` to your certificate and key files (PEM); the
  session cookie is then marked Secure automatically.
- Nothing is reachable from the internet; only devices on the same network can connect.

## Current product scope

Implemented: sign-in and roles; master repository with modules, versions,
approvals, duplicate detection and attachments; Excel import preview, validation,
history and compatible export; test cycles with snapshots; module and individual
assignment with a drag-and-drop board; My Work and execution with evidence and full
history; cycle comparison; dashboard and Excel reports; tamper-evident audit log;
test plans with exit criteria and release readiness; reusable test suites;
defect lifecycle with retest tracking; requirement coverage.

Known limits:
- Excel workbooks are the database, so each save rewrites the workbook it changes.
  Measured with a year of simulated data (3,000 test cases, 6,000 runs, 50,000+
  history rows): reading pages takes milliseconds, the dashboard under a second,
  and recording a result about 0.7–1.5 s. Completing old cycles keeps it that way.
- One TMExpert server per `application-data` folder. Sessions are kept in memory, so
  everyone signs in again after a restart.
- Two roles (Admin, Team Member); the permission table in `app/auth.py` is built
  for more.

# TMExpert — QA Platform Audit, Gap Analysis and Target Workflow

Audit date: 2026-11-22 · Code version: `6af6976` · Data: live `application-data` (read-only)

**Status:** gaps C1, H1–H4, M1 and M2 are implemented (see README → *Releases: plans, suites, defects and coverage*). Low-priority items in section 4 remain future work.

## 1. Existing architecture

- **Server:** Python standard library HTTP server (`server.py`) with services in `app/`
  (storage, auth, workflow/approvals, cycles, assignments, execution, attachments,
  comparison, reports, audit log, Excel import). Role checks on every request.
- **Storage:** Excel-only. 21 workbooks under `application-data/`, written atomically,
  validated, backed up, audited; history workbooks are append-only and split into parts.
- **Front end:** single-page app (`web/app.js`, `web/styles.css`), no framework.
- **Tests:** 135 automated tests (`tests/`), standard `unittest`.

### What already exists (and works)

| Area | Implemented as | Assessment |
|---|---|---|
| Repository | Projects → modules (nested folders) → test cases with preconditions, steps (action / expected / test data), expected result, priority, type, tags, automation status (data only) | Solid |
| Versioning | Every content change creates a version; full version history with field diffs | Solid |
| Review | Draft → Pending → Approved / Rejected (reason) → Deprecated; module approval; approval history | Solid |
| Test case reuse | A cycle holds one **test run** per test case (`test_runs`), pinned to the case version; the test case itself is never copied; a case can be in any number of cycles; unique per cycle | **Correct** — matches TestRail/Xray "test vs test run" model |
| Execution | Assignment (module defaults + individual), My Work, step results, actual result, notes, evidence files, attempts, append-only execution history, who/when | Solid |
| Comparison | Case-by-cycle matrix, regressions/fixes/reopened, case history and cycle timeline | Solid |
| Reporting | Dashboard (filters, module/tester progress, regression trend), Excel reports | Good, but no release level |
| Governance | Sign-in, roles, audit log with tamper detection, import history | Solid |

### What exists only partly

| Area | Current state |
|---|---|
| **Test plans** | Workbook has name, release, build, dates, scope, status. The page is a raw table of the first 7 fields; no detail page, no editing, no progress. A plan cannot show its cycles. |
| **Plan ↔ cycle link** | Indirect: choosing a plan in the cycle wizard creates a hidden `test_suites` row per cycle that points to the plan. Cycles created without a plan have no release context. |
| **Test suites** | Only those hidden per-cycle link rows. No reusable, named collections. |
| **Defects** | `Defects.xlsx` exists (summary, severity, priority, status, assignee) but is never used. Executions store defect IDs as free text, so there is no defect status, no "fixed → retest" signal, and the defects report is a list of strings. |
| **Requirements** | 27 requirements, all 68 active cases linked (via import). The page is a raw table; links cannot be seen or edited in the UI; no coverage view. |
| **Automation status** | Stored on every case (all "Not Automated"), not shown or editable. |

## 2. Industry benchmark (TestRail, Xray, Zephyr, PractiTest) — adapted, not copied

| Practice | TMExpert today |
|---|---|
| Reusable test definitions, independent executions | ✅ Yes (test case → test run per cycle) |
| Versioned cases, review workflow | ✅ Yes |
| Cycles typed by objective (Smoke, Sanity, Regression, Functional, Integration, UAT, Retest) with build/environment | ✅ Yes (type is free-form via "Custom") |
| Release/sprint plan grouping several cycles | ⚠️ Data exists; link is indirect and invisible |
| Reusable suites ("Payment smoke set") used across releases | ❌ No |
| Assignment, step-by-step execution, evidence, history | ✅ Yes |
| Defects as records with status, linked to failed executions | ❌ Free-text IDs only |
| Retest after fix (from failed runs / fixed defects) | ⚠️ Possible by hand (new cycle, re-select cases); no guided flow, no fixed-defect signal |
| Release-level progress, planned vs executed, readiness against exit criteria | ❌ Cycle/project level only |
| Requirement coverage / traceability | ⚠️ Links exist in data, no UI |

## 3. Gap analysis

### Critical — fundamental workflow
**C1. The planning layer is disconnected from execution.**
- *Now:* a cycle reaches its plan only through a hidden suite row created per cycle; cycles can exist with no release; the plan page cannot list its cycles or show progress.
- *Should:* every cycle belongs to a plan (the release/sprint context) directly, and a plan shows its cycles, scope and progress.
- *Why insufficient:* "How is Release 2.5 doing?" cannot be answered; the Repository → Plan → Cycle lifecycle is broken in the middle.
- *Change:* add `plan_id` to cycles (migrated from the existing suite links); plan list + plan detail pages; cycle wizard keeps plan selection; cycles without a plan stay valid ("No plan").
- *Affected:* `app/storage.py` (column + migration), `app/cycles.py`, new plan service, `server.py`, plans UI, cycle wizard.

### High — needed by professional QA teams
**H1. No reusable test suites.**
- *Now:* every cycle is built by re-selecting modules/cases.
- *Should:* saved, named suites (by modules and/or picked cases) that any cycle can start from, across releases.
- *Change:* make `test_suites` real: `kind` (`reusable` vs existing `cycle_link`), `module_ids`, `case_ids`, `include_submodules`; plan link becomes optional; existing link rows are kept and hidden. Suites page; "Start from suite" in the cycle wizard; a suite resolves to cases at use time (new approved cases in its modules are picked up), and the cycle still snapshots versions.
- *Affected:* storage (columns, required fields), new suite service, cycle wizard, UI.

**H2. Defects are not first-class.**
- *Now:* free text on executions; `Defects.xlsx` unused.
- *Should:* defect records (key, summary, severity, priority, status, assignee, environment/build, optional external Jira key/URL) linked to the failed executions they came from; status workflow Open → In Progress → Fixed → Verified → Closed, with Reopened.
- *Change:* use the existing defects workbook (+ external key/URL, found/fixed build, resolved/closed dates); "Log defect" from a failed result (pre-filled); link to an existing defect; Defects page. Existing free-text IDs keep working: they are shown, and can be registered as defect records on demand (no data rewritten).
- *Affected:* storage, new defect service, execution screen, reports, UI.

**H3. No guided retest.**
- *Now:* retesting means creating a cycle and re-selecting cases by hand.
- *Should:* "Retest" creates a Retesting cycle from a cycle's Failed/Blocked runs or from runs whose defects are Fixed, linked to the source cycle; when a retest passes, the linked defect can be marked Verified.
- *Change:* `source_cycle_id` on cycles; retest action on cycles and on fixed defects; reuse the cycle creation path.
- *Affected:* cycles service, defects service, execution UI.

**H4. No release-level reporting or readiness.**
- *Now:* dashboard and reports are per cycle/project.
- *Should:* per plan: planned vs executed across its cycles (latest result per case), pass/fail/blocked/not run, tester and module progress, open defects by severity, and a readiness verdict against the plan's own exit criteria.
- *Change:* exit criteria stored per plan (min execution %, min pass %, max open critical/high defects, no blocked — all editable, defaults provided); plan report page and Excel export.
- *Affected:* plan service, reports, UI.

### Medium — workflow improvements
- **M1. Plans and Requirements pages are raw data tables.** Replace Plans with real list/detail (C1). Requirements: proper list with coverage (linked cases and their latest results).
- **M2. Case form hides automation status and requirement links** that already exist in the data. Expose both.

### Low — advanced (not recommended now)
- Configurable custom fields and statuses; shared/called steps; parameterised (data-driven) cases; Jira API sync (external keys/links in H2 cover the common need). Severity on test cases (severity belongs to defects).

## 4. Target workflow

```
Test Repository (cases, versions, approval)
   → Test Suites (reusable named selections of modules / cases)
   → Test Plan  (= release or sprint: release, dates, scope, exit criteria)
   → Test Cycles (belong to a plan; type, build, environment; built from suites / modules / cases;
                  one test run per case, pinned to its version)
   → Execution  (assignment, step results, evidence, attempts, history)
   → Defects    (records linked to failed runs; status workflow; external key)
   → Retest     (Retesting cycle from failed runs / fixed defects, linked to the source cycle)
   → Reports    (cycle, plan/release, readiness against exit criteria)
```

Decisions:
- **No separate Release entity:** a Test Plan *is* the release/sprint testing effort (it already has a `release` field). Reports group by plan, and by release text across plans.
- **The test case stays the single reusable source;** a test run is the per-cycle execution record (already the case).
- **All storage changes are additive columns** with defaults and migrations; no workbook is restructured, no data deleted.

## 5. Proposed build order
1. C1 — plan ↔ cycle link, plan list and detail.
2. H1 — reusable suites; "start cycle from suite".
3. H2 — defects as records, linking from executions.
4. H3 — retest cycles.
5. H4 — release readiness and plan report.
6. M1/M2 — requirements coverage page; automation and requirement fields on the case form.

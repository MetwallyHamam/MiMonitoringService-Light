# MiMonitoringService — Current Project Plan

Last updated: 14 November 2026

This file is the authoritative tracker for the current product scope. Completed implementation
history, superseded ideas, release hashes, and repeated session logs have been removed.

## Current position

- **Current milestone:** SQL monitoring hardening and release validation
- **Latest completed work:** SQL session/blocking monitoring, guarded multi-session termination,
  lightweight backup-file assurance, and senior-review safety fixes
- **Automated verification:** Ruff passed; 108 tests passed in 125.60 seconds
- **Current release:** v0.1.1 is the last packaged release
- **Workspace state:** Contains verified changes newer than v0.1.1
- **Next release:** Build only after live SQL validation and final interactive visual review

## Product scope

### Infrastructure monitoring

- Windows Service monitoring and guarded recovery
- IIS application-pool monitoring and guarded recovery
- Disk, CPU, and memory monitoring with sustained thresholds
- HTTP/API health checks
- Maintenance windows and scheduled actions
- Explainable health score, incidents, recoveries, audit events, and retention
- Email notifications, daily/monthly summaries, and branded PDF/Word reports
- Portable GUI and Windows Service executables

### SQL Operations

Only the following SQL capabilities are in scope:

1. Database growth per day and month, measured from the first available monthly baseline
2. Native SQL backup compliance from `msdb`
3. SQL Agent job monitoring and guarded manual execution
4. Blocking sessions, blocking chains, query text, and source attribution
5. Deadlocks and likely root cause
6. Low-overhead performance monitoring
7. SQL Server and database configuration-change monitoring
8. Live user-session inventory with filters and guarded multi-session termination

Continuous plan-cache ranking, profiler-style tracing, and unrelated SQL administration features are
not in scope.

### Lightweight backup-file assurance

Backup Files remains under Assurance because it does not require a SQL connection. It supports:

- Folder or individual-file monitoring
- Filename pattern and keyword matching
- Maximum calendar age
- Scheduled daily checks and manual checks
- Evidence, status, incidents, notifications, and acknowledgement

This complements but does not replace native SQL backup compliance.

## Implementation status

| Area | Status | Notes |
|---|---|---|
| Application foundation and SQLite persistence | Complete | Schema migrations preserve the customer database |
| Windows Services, IIS, disks, CPU, memory, and API checks | Complete | Guarded actions and audit trail implemented |
| Scheduling, maintenance windows, and email queue | Complete | Persistent claims, retries, and cooldowns implemented |
| Dashboard, events, diagnostics, and reports | Complete | Modern PDF/Word dashboard and evidence tables implemented |
| Database capacity and growth | Complete | Daily evidence, monthly baseline, file risks, and forecasts |
| Native backup compliance and recovery evidence | Complete | Full/differential/log history and restore evidence |
| SQL Agent monitoring | Complete | Jobs, steps, schedules, duration, failures, and guarded run action |
| SQL performance monitoring | Complete | CPU, sessions, memory grants, blocking, I/O, waits, and baselines |
| Blocking and live sessions | Complete | Query/source evidence, filters, history, and guarded multi-kill |
| Deadlock evidence | Complete | Existing `system_health` evidence with redaction and deduplication |
| SQL configuration changes | Complete | Server and database snapshot comparison |
| Backup-file assurance | Complete | Credential-free scheduled/manual checks and acknowledgement |
| Installer and signed distribution | Blocked | Requires installer compiler and code-signing certificate |
| Live SQL compatibility validation | Pending | Requires approved SQL Server and least-privilege credentials |
| Interactive packaged visual QA | Pending | Required at common Windows DPI/scaling levels |

## Safety rules already enforced

- SQL session termination is disabled by default per profile.
- Every termination requires explicit selection and confirmation.
- SPID identity is revalidated using login time, login, host, and application before `KILL`.
- System sessions, low-numbered SPIDs, and MiMonitoringService connections are protected.
- Every session-termination result is audited.
- Manual SQL Agent execution is disabled by default and limited to explicitly monitored jobs.
- Query literals and comments are normalized before persistence.
- SQL collectors isolate permission failures so one optional DMV does not break connection monitoring.
- Backup acknowledgements reference exact evidence and expire when newer evidence is collected.
- Existing customer data is retained during executable replacement and schema migration.

## Remaining work before the next release

### Required

- [ ] Validate supported SQL Server versions using the intended least-privilege account.
- [ ] Validate `VIEW SERVER STATE` or `VIEW SERVER PERFORMANCE STATE` behavior.
- [ ] Validate `system_health` deadlock XML variants.
- [ ] Test a sleeping blocker with an open transaction and confirm most-recent query attribution.
- [ ] Test active blocking chains and stored procedure/trigger/function source attribution.
- [ ] Test SPID turnover and confirm reused sessions are rejected before termination.
- [ ] Test confirmed single-session and multi-session termination in non-production.
- [ ] Confirm native backup compliance and SQL Agent visibility with production-like permissions.
- [ ] Review the packaged GUI at 100%, 125%, 150%, and 200% scaling.
- [ ] Generate and inspect one PDF and one Word report from the packaged application.
- [ ] Increment the patch version and build a new full private release package.
- [ ] Verify executable versions, manifest hashes, clean startup, database migration, and rollback copy.

### Recommended hardening

- [ ] Run SQL session termination in a background worker so slow providers cannot freeze the GUI.
- [ ] Consolidate repeated blocking samples into start/change/end blocking episodes.
- [ ] Add an optional policy to disable persisted query text for sensitive environments.
- [ ] Perform a long-running service, SMTP outage, database-lock, and restart soak test.
- [ ] Complete installed upgrade and uninstall validation.

### External blockers

- Approved non-production SQL Server endpoint and representative workload
- Least-privilege monitoring credentials and separately approved termination permission
- Inno Setup compiler for installer generation
- Millensys production code-signing certificate

## Release procedure

1. Complete every required validation item above.
2. Run Ruff and the complete automated test suite.
3. Perform interactive GUI and report visual QA.
4. Increment the patch version consistently in package and installer metadata.
5. Build GUI and service executables into a new versioned release directory.
6. Build the full private ZIP without modifying an older release package.
7. Verify executable versions and SHA-256 manifest values independently.
8. Test replacement while retaining `Customer\data` and confirm automatic schema migration.
9. Record the final version, test count, artifact paths, hashes, and known limitations here.

## Definition of done

The next release is complete when:

- All required validation items pass.
- No critical or high-severity review finding remains open.
- The complete automated suite passes.
- The packaged GUI and service start cleanly on a supported Windows system.
- Existing customer configuration and evidence survive the upgrade.
- SQL actions remain permission-gated, confirmed, revalidated, and audited.
- PDF and Word reports are readable and correctly aligned.
- A new versioned private release package and verified manifest are produced.

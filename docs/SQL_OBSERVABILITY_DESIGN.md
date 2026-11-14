# SQL Server Observability Design

## Purpose

This document defines the implementation contract for Phase 9 database monitoring. The objective is
to provide useful database evidence without turning MiMonitoringService into a source of SQL Server
load, changing database state, or confusing collection failures with database failures.

## Collector contract

Every SQL collector must:

1. Be read-only unless it is a separately authorized manual action.
2. Have an explicit query timeout and bounded result size.
3. Run on its own configured interval rather than every agent health cycle.
4. Isolate errors per profile, database, and metric domain.
5. Record the last successful sample, last attempt, duration, and clear failure classification.
6. Distinguish Connected, Healthy, Attention, Critical, Stale, No permission, Unsupported, and
   Unavailable.
7. Store normalized numeric evidence, not formatted UI strings.
8. Avoid credentials, query parameters, and unredacted sensitive SQL text.
9. Use sustained thresholds, baselines, cooldown, deduplication, and recovery for notifications.
10. Remain compatible with the supported SQL Server capability matrix.

## Collection tiers

| Tier | Default interval | Examples | Design limit |
|---|---:|---|---|
| Fast health | 30 seconds | Connectivity and database state | One bounded query per profile |
| Operational | 5 minutes | Sessions, blocking, active jobs, memory grants | Bounded rows and ten-second timeout |
| Capacity | 15 minutes | Files, utilization, volumes, TempDB | Inventory query only; no file scans |
| Historical | 60 minutes | Wait deltas, capacity forecast, job baselines | Aggregate before long-term retention |
| Assurance | Daily/scheduled | Backup SLA, restore evidence | Run after the expected backup window |

Administrators may increase intervals. Production minimums must prevent high-frequency expensive
queries.

## Capacity data model

`sql_file_samples` stores one row per SQL database file per collection occurrence:

- Profile and database identity
- Logical name, type, and physical path
- Allocated, used, and free MB
- Utilization percentage
- Configured maximum size
- Growth value and percentage/fixed mode
- Hosting volume, total MB, and free MB
- Plain-language risk interpretation

Recent detailed samples follow diagnostic retention. Future downsampling will retain daily start,
end, minimum, maximum, and average values before detailed samples expire.

## Failure classification

| State | Meaning | UI behavior |
|---|---|---|
| Connected | Login and base SQL state query succeeded | Connection card is healthy |
| Healthy | Collector succeeded and no configured risk was found | Green state with sample time |
| Attention | Collector succeeded and found actionable capacity/configuration risk | Amber state with evidence |
| Critical | Collector found immediate availability or exhaustion risk | Red state and prioritized incident |
| Stale | A previous sample exists but is older than the allowed freshness window | Show last sample; do not present it as current |
| No permission | SQL connection works but required metadata permission is absent | Explain missing permission separately |
| Unsupported | SQL version/edition lacks the required capability | Explain capability; do not alert as outage |
| Unavailable | Collector failed for another bounded reason | Preserve other monitoring domains |

## UI information architecture

The Database navigation entry opens a simple overview:

1. Instance selector and data freshness.
2. Connection, capacity health, file count, and risk-count cards.
3. File table with allocated, used, free, utilization, autogrowth, maximum, volume headroom, and a
   plain-language interpretation.
4. Later tabs will add Backup Assurance, Agent Jobs, Performance, and Diagnostics while preserving
   the selected instance, database, and duration.

Configuration remains under Assurance until Phase 9 moves SQL profile setup into a dedicated guided
configuration workflow. Operational views never expose protected passwords.

## Retention and downsampling

- Detailed SQL file samples use the existing diagnostic retention period initially.
- Cleanup executes in the existing bounded daily retention job.
- Phase 9 historical aggregation will be added before supporting periods longer than detailed
  retention.
- Report generation must use aggregates when detailed samples are unavailable and disclose the
  evidence resolution.

## Safety gates

- No shrink, autogrowth change, file move, backup, restore, kill-session, plan forcing, or Agent job
  execution is part of automatic monitoring.
- A future manual action requires separate permissions, confirmation, an action claim, timeout,
  result capture, and audit event.
- Query text and diagnostic evidence must pass redaction before persistence, email, export, or UI.


# SQL Monitoring Permission Matrix

## Principle

Use a dedicated monitoring login with the minimum permissions required for the selected features.
Do not grant `sysadmin` to MiMonitoringService. SQL authentication passwords remain protected by
Windows DPAPI; Windows authentication uses the Windows Service account identity.

## Phase 9 permission matrix

| Monitoring capability | Minimum intended access | Behavior when unavailable |
|---|---|---|
| Connect and identify server | `CONNECT SQL` and permission to connect to the configured database | Connection failed |
| Database state and file inventory | `VIEW ANY DATABASE` plus access to the monitored database | Database not visible or No permission |
| File used/free space | Database access and visibility of `sys.database_files`/`FILEPROPERTY` | Capacity state is No permission |
| Volume mapping | `VIEW SERVER STATE` on older versions or the version-appropriate server performance-state permission | File data remains; volume values show unavailable |
| Backup history | Read access to required `msdb` backup history views/tables | Backup Assurance shows No permission |
| SQL Agent jobs | SQLAgentReaderRole in `msdb`, or a narrower approved equivalent | Jobs show No permission; connection remains healthy |
| Sessions, waits, blocking, I/O | Version-appropriate `VIEW SERVER STATE` or `VIEW SERVER PERFORMANCE STATE` | Performance domain shows No permission |
| Query Store | `VIEW DATABASE STATE` for each enabled database | Query Store section shows Unsupported or No permission |
| Extended Events evidence | Permission to view the approved event session/file target | Deadlock/timeout evidence shows No permission |
| Manual job execution | Not granted by default; explicit approved Agent role/proxy policy | Run action hidden or disabled |

## Recommended validation workflow

1. Create the monitoring identity according to the client's authentication policy.
2. Grant only connectivity and the permissions needed for enabled monitoring domains.
3. Use Test connection and capability discovery in the GUI.
4. Review each domain state. A missing optional permission must not appear as a SQL outage.
5. Record the approved permission set in the client deployment evidence.
6. Revalidate after SQL Server upgrades, service-account changes, or permission reviews.

## Security requirements

- Never include passwords in connection-test errors, logs, events, email, or reports.
- Never store unredacted query literals or parameter values.
- Do not request `sysadmin` as a troubleshooting shortcut.
- Keep manual-action permissions separate from read-only monitoring permissions.
- Audit profile changes, permission capability changes, and every authorized manual action.


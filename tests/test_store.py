from millensys_guardian.store import Store


def test_inverted_sql_cpu_history_is_invalidated_once(tmp_path):
    path = tmp_path / "cpu-migration.db"
    store = Store(path)
    with store.connect() as db:
        db.execute("DELETE FROM settings WHERE key='sql_cpu_process_utilization_v3'")
        db.execute(
            "INSERT INTO sql_performance_samples(collected_at,profile_id,user_connections,active_sessions,sql_cpu_percent,memory_grants_pending,blocked_sessions,longest_blocked_seconds,details) VALUES(?,?,?,?,?,?,?,?,?)",
            ("2026-11-14T00:00:00+00:00", "prod", 1, 0, 100, 0, 0, 0, "{}"),
        )

    migrated = Store(path)

    assert migrated.sql_performance_samples("2026-11-01T00:00:00+00:00")[0]["sql_cpu_percent"] is None
    assert migrated.get("sql_cpu_process_utilization_v3") is True


def test_store_round_trip(tmp_path):
    store = Store(tmp_path / "test.db")
    store.set("answer", {"value": 42})
    assert store.get("answer") == {"value": 42}


def test_target_upsert_is_unique(tmp_path):
    store = Store(tmp_path / "test.db")
    store.upsert_target("service", "Spooler", "Print Spooler")
    store.upsert_target("service", "Spooler", "Printer")
    assert len(store.targets("service")) == 1
    assert store.targets("service")[0]["display_name"] == "Printer"


def test_target_can_be_disabled_without_losing_configuration(tmp_path):
    store = Store(tmp_path / "test.db")
    original = {"auto_restart": True, "retry_count": 4}
    store.upsert_target("service", "Spooler", "Print Spooler", original)

    store.set_target_enabled("service", "Spooler", False)

    target = store.target("service", "Spooler")
    assert target is not None
    assert target["enabled"] == 0
    assert target["config"] == '{"auto_restart": true, "retry_count": 4}'


def test_duplicate_health_events_are_suppressed(tmp_path):
    store = Store(tmp_path / "dedup.db")
    store.add_event_deduplicated("warning", "disk", "C:", "low")
    store.add_event_deduplicated("warning", "disk", "C:", "low")

    assert len(store.recent_events()) == 1


def test_failed_action_claim_can_be_retried(tmp_path):
    store = Store(tmp_path / "retry.db")
    assert store.claim_action("job", "service", "SQL", "restart", "2026-11-13T01:00Z")
    store.finish_action("job", False, "failed")
    with store.connect() as db:
        db.execute(
            "UPDATE action_claims SET claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE claim_key='job'"
        )

    assert store.claim_action(
        "job",
        "service",
        "SQL",
        "restart",
        "2026-11-13T01:00Z",
        retry_failed=True,
    )


def test_disabling_target_clears_active_alert(tmp_path):
    store = Store(tmp_path / "alerts.db")
    store.upsert_target("service", "SQL", "SQL", {})
    with store.connect() as db:
        db.execute(
            "INSERT INTO alert_state(fingerprint,active,last_seen,category,target,condition,message) "
            "VALUES('sql',1,'2026-11-13T00:00:00+00:00','service','SQL','down','down')"
        )

    store.set_target_enabled("service", "SQL", False)

    assert store.active_alerts() == []


def test_invalid_legacy_iis_error_targets_are_removed(tmp_path):
    path = tmp_path / "legacy.db"
    store = Store(path)
    store.upsert_target("pool", "ERROR ( hresult:80004001", "ERROR", {})
    store.upsert_target("pool", "RealPool", "Real Pool", {})

    migrated = Store(path)

    assert migrated.target("pool", "ERROR ( hresult:80004001") is None
    assert migrated.target("pool", "RealPool") is not None


def test_sql_blocking_evidence_round_trip(tmp_path):
    store = Store(tmp_path / "blocking.db")
    store.add_sql_blocking_evidence("primary", "2026-11-14T10:00:00+00:00", [{
        "session_id": 61, "blocking_session_id": 60, "status": "suspended",
        "wait_type": "LCK_M_X", "wait_ms": 12000, "database_name": "ERP",
        "application_name": "ERP API", "host_name": "APP01", "login_name": "erp_user",
        "source_type": "Stored procedure", "source_name": "[ERP].[dbo].[PostInvoice]",
        "query_text": "EXEC dbo.PostInvoice @id=?", "transaction_age_seconds": 90,
        "open_transactions": 1, "is_blocker": False,
    }])

    rows = store.sql_blocking_evidence("2026-11-14T00:00:00+00:00", "primary")
    assert len(rows) == 1
    assert rows[0]["session_id"] == 61
    assert rows[0]["source_type"] == "Stored procedure"
    assert rows[0]["query_text"] == "EXEC dbo.PostInvoice @id=?"

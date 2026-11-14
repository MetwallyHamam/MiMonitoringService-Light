from datetime import UTC, datetime, timedelta

from millensys_guardian.sql_jobs import duration_seconds, job_intelligence, schedule_assessment


def execution(execution_id, status, minutes, age_hours):
    return {
        "execution_id": execution_id, "status": status,
        "duration_seconds": minutes * 60,
        "run_at": (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat(),
    }


def test_duration_parser_supports_sql_agent_hhmmss():
    assert duration_seconds(10203) == 3723
    assert duration_seconds(None) == 0


def test_job_intelligence_calculates_success_percentiles_and_failure_streak():
    rows = [
        execution(6, "Failed", 40, 1), execution(5, "Failed", 10, 2),
        execution(4, "Succeeded", 10, 3), execution(3, "Succeeded", 9, 4),
        execution(2, "Succeeded", 11, 5), execution(1, "Succeeded", 10, 6),
    ]
    result = job_intelligence(rows, overdue_hours=24)
    assert result["success_percent"] == 66.67
    assert result["median_seconds"] == 600
    assert result["failure_streak"] == 2
    assert result["abnormal_duration"] is True
    assert result["overdue"] is False


def test_job_without_recent_history_is_overdue():
    assert job_intelligence([], overdue_hours=12)["overdue"] is True
    assert job_intelligence([execution(1, "Succeeded", 2, 30)], overdue_hours=12)["overdue"] is True


def test_schedule_assessment_distinguishes_missed_overlap_and_disabled():
    past = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    base = {"job_enabled": True, "schedule_count": 1, "enabled_schedule_count": 1, "next_run": past}
    assert schedule_assessment({**base, "status": "Failed"}, grace_minutes=10)["status"] == "Missed occurrence"
    assert schedule_assessment({**base, "status": "Running"}, grace_minutes=10)["overlap"] is True
    assert schedule_assessment({**base, "job_enabled": False})["status"] == "Job disabled"


def test_failure_window_and_recovery_transition_are_explicit():
    now = datetime(2026, 11, 14, 12, tzinfo=UTC)
    rows = [
        {"status": "Succeeded", "duration_seconds": 60, "run_at": (now - timedelta(hours=1)).isoformat()},
        {"status": "Failed", "duration_seconds": 60, "run_at": (now - timedelta(hours=2)).isoformat()},
        {"status": "Failed", "duration_seconds": 60, "run_at": (now - timedelta(hours=30)).isoformat()},
    ]
    result = job_intelligence(rows, now=now)
    assert result["failures_24h"] == 1
    assert result["recovered"] is True
    assert result["failure_streak"] == 0

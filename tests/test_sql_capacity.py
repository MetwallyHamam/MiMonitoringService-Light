from millensys_guardian.sql_capacity import capacity_forecasts, forecast_text, tempdb_balance
from millensys_guardian.store import Store


def file_sample(allocated, used=500, volume_free=10000):
    return {
        "logical_name": "ERP_Data",
        "file_type": "ROWS",
        "physical_path": r"D:\SQLData\ERP.mdf",
        "allocated_mb": allocated,
        "used_mb": used,
        "free_mb": allocated - used,
        "utilization_percent": used / allocated * 100,
        "max_size_mb": 2000,
        "growth_value": 128,
        "growth_is_percent": False,
        "volume_mount": "D:\\",
        "volume_total_mb": 100000,
        "volume_free_mb": volume_free,
        "risk": "",
    }


def test_daily_aggregate_and_growth_event_are_persisted(tmp_path):
    store = Store(tmp_path / "capacity.db")
    assert not store.add_sql_file_samples(
        "prod", "Production", "ERP", [file_sample(1000)], "2026-11-01T08:00:00+00:00"
    )
    events = store.add_sql_file_samples(
        "prod", "Production", "ERP", [file_sample(1128, 700, 9800)],
        "2026-11-01T12:00:00+00:00",
    )

    assert events[0]["growth_mb"] == 128
    daily = store.sql_capacity_daily("2026-11-01")
    assert len(daily) == 1
    assert daily[0]["start_allocated_mb"] == 1000
    assert daily[0]["end_allocated_mb"] == 1128
    assert daily[0]["min_used_mb"] == 500
    assert daily[0]["max_used_mb"] == 700
    assert daily[0]["samples"] == 2
    assert store.sql_growth_events("2026-11-01T00:00:00+00:00")[0]["growth_mb"] == 128


def test_capacity_forecast_reports_limit_and_confidence():
    rows = [
        {
            "profile_id": "prod", "database_name": "ERP", "logical_name": "ERP_Data",
            "day": "2026-11-01", "end_allocated_mb": 1000, "max_size_mb": 2000,
            "min_volume_free_mb": 4000,
        },
        {
            "profile_id": "prod", "database_name": "ERP", "logical_name": "ERP_Data",
            "day": "2026-11-08", "end_allocated_mb": 1070, "max_size_mb": 2000,
            "min_volume_free_mb": 3930,
        },
        {
            "profile_id": "prod", "database_name": "ERP", "logical_name": "ERP_Data",
            "day": "2026-11-15", "end_allocated_mb": 1140, "max_size_mb": 2000,
            "min_volume_free_mb": 3860,
        },
    ]

    forecast = capacity_forecasts(rows)[("prod", "ERP", "ERP_Data")]

    assert forecast["growth_mb_per_day"] == 10
    assert forecast["days_to_limit"] == 86
    assert forecast["limit"] == "file maximum"
    assert forecast["confidence"] == "High"
    assert "86 days" in forecast_text(forecast)


def test_capacity_forecast_requires_multiple_days():
    rows = [
        {
            "profile_id": "prod", "database_name": "ERP", "logical_name": "ERP_Data",
            "day": "2026-11-01", "end_allocated_mb": 1000, "max_size_mb": 2000,
            "min_volume_free_mb": 4000,
        }
    ]
    forecast = capacity_forecasts(rows)[("prod", "ERP", "ERP_Data")]
    assert forecast["confidence"] == "Insufficient"
    assert forecast_text(forecast) == "Collecting baseline"


def test_tempdb_balance_explains_size_and_growth_mismatch():
    files = [
        {"file_type": "ROWS", "allocated_mb": 1024, "growth_is_percent": False, "growth_value": 128},
        {"file_type": "ROWS", "allocated_mb": 512, "growth_is_percent": False, "growth_value": 64},
    ]
    result = tempdb_balance(files, "tempdb")
    assert result["status"] == "Attention"
    assert "sizes differ" in result["message"]
    assert "inconsistent" in result["message"]
    assert tempdb_balance(files, "ERP") is None

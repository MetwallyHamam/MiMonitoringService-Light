from datetime import UTC, datetime

from millensys_guardian.time_utils import parse_timestamp, server_time_text


def test_server_time_text_converts_utc_to_configured_server_timezone():
    config = {"agent": {"timezone": "Africa/Cairo"}}

    assert server_time_text("2026-11-14T10:00:00+00:00", config) == "2026-11-14 12:00:00"


def test_parse_timestamp_treats_legacy_naive_values_as_utc():
    assert parse_timestamp("2026-11-14T10:00:00") == datetime(
        2026, 11, 14, 10, tzinfo=UTC
    )

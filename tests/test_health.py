from millensys_guardian.health import SustainedThresholds


def test_threshold_requires_sustained_duration_and_recovers_once():
    tracker = SustainedThresholds()

    assert not tracker.update("cpu", True, 100.0, 120).sustained
    assert not tracker.update("cpu", True, 219.9, 120).sustained
    assert tracker.update("cpu", True, 220.0, 120).sustained
    assert tracker.update("cpu", False, 221.0, 120).recovered
    assert not tracker.update("cpu", False, 222.0, 120).recovered


def test_short_spike_is_discarded():
    tracker = SustainedThresholds()

    tracker.update("memory", True, 10.0, 60)
    assert not tracker.update("memory", False, 20.0, 60).recovered
    assert not tracker.update("memory", True, 30.0, 60).sustained

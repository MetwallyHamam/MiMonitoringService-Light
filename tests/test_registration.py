from millensys_guardian.registration import validate_operator, validate_recipients


def test_millensys_domain_is_required():
    config = {"required_domain": "millensys.com", "mode": "domain"}

    assert validate_operator("engineer@millensys.com", config).valid
    assert not validate_operator("engineer@example.com", config).valid
    assert not validate_operator("", config).valid


def test_allowlist_mode_requires_registered_address():
    config = {
        "required_domain": "millensys.com",
        "mode": "allowlist",
        "registered_users": ["registered@millensys.com"],
    }

    assert validate_operator("REGISTERED@millensys.com", config).valid
    result = validate_operator("unknown@millensys.com", config)
    assert not result.valid
    assert "not registered" in result.message


def test_notification_and_report_recipients_require_millensys_domain():
    config = {"required_domain": "millensys.com"}

    assert validate_recipients(["operator@millensys.com"], config).valid
    assert validate_recipients([], config, required=False).valid
    assert not validate_recipients([], config).valid
    assert not validate_recipients(["operator@example.com"], config).valid
    assert not validate_recipients(["not-an-email"], config).valid

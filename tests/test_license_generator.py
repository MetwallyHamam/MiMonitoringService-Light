from support_tools.license_generator import (
    OTP_EXPIRY_SECONDS,
    OTP_REFERENCE_EMAIL,
    build_otp_message,
)


def test_registration_otp_is_branded_html_with_hidden_reference_recipient() -> None:
    message = build_otp_message(
        sender_name="MiMonitoringService Licensing",
        sender_email="licensing@example.com",
        recipient="operator@millensys.com",
        otp_code="123456",
        client_name="Example & Client",
        server_name="SERVER-01",
    )

    assert OTP_EXPIRY_SECONDS == 3600
    assert message["To"] == "operator@millensys.com"
    assert message["Bcc"] == OTP_REFERENCE_EMAIL
    assert OTP_REFERENCE_EMAIL not in str(message["To"])
    assert message.is_multipart()

    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "expires in one hour" in plain
    assert "MILLENSYS" in html
    assert "Verify your registration" in html
    assert "123456" in html
    assert "Example &amp; Client" in html

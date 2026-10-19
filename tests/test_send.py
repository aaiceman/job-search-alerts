import smtplib

import pytest

from jobsearch.send import build_message, load_env, send_email

ENV = {"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"}


def test_load_env(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nGMAIL_ADDRESS=me@gmail.com\nGMAIL_APP_PASSWORD="abcd efgh"\n', encoding="utf-8")
    assert load_env(p) == {"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "abcd efgh"}


def test_build_message_self_addressed_multipart():
    msg = build_message("Subj", "<b>hi</b>", "hi", ENV)
    assert msg["From"] == msg["To"] == "me@gmail.com" and msg["Subject"] == "Subj"
    assert msg.is_multipart()


class FakeSMTP:
    attempts = 0
    fail_times = 0
    sent = []

    def __init__(self, host, port, timeout):
        assert (host, port) == ("smtp.gmail.com", 465)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        FakeSMTP.attempts += 1
        if FakeSMTP.attempts <= FakeSMTP.fail_times:
            raise smtplib.SMTPServerDisconnected("flaky")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg["Subject"])


def test_send_retries_then_succeeds():
    FakeSMTP.attempts, FakeSMTP.fail_times, FakeSMTP.sent = 0, 2, []
    sleeps = []
    send_email("S", "<b>h</b>", "h", ENV, smtp_factory=FakeSMTP, sleep=sleeps.append)
    assert FakeSMTP.sent == ["S"] and sleeps == [5, 30]


def test_send_gives_up_after_retries():
    FakeSMTP.attempts, FakeSMTP.fail_times, FakeSMTP.sent = 0, 99, []
    with pytest.raises(RuntimeError, match="send failed"):
        send_email("S", "h", "h", ENV, smtp_factory=FakeSMTP, sleep=lambda s: None)
    assert FakeSMTP.attempts == 4


def test_load_env_handles_bom(tmp_path):
    p = tmp_path / ".env"
    p.write_text("GMAIL_ADDRESS=me@gmail.com\nGMAIL_APP_PASSWORD=pw\n", encoding="utf-8-sig")
    assert load_env(p)["GMAIL_ADDRESS"] == "me@gmail.com"


@pytest.mark.parametrize("missing", ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"])
def test_load_env_requires_credentials_without_leaking_values(tmp_path, missing):
    p = tmp_path / ".env"
    lines = {"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "s3cretvalue"}
    lines[missing] = ""
    p.write_text("".join(f"{k}={v}\n" for k, v in lines.items()), encoding="utf-8")
    with pytest.raises(RuntimeError, match=f"missing {missing} in") as ei:
        load_env(p)
    assert "s3cretvalue" not in str(ei.value) and "me@gmail.com" not in str(ei.value)


def test_default_factory_verifies_certificates(monkeypatch):
    import ssl
    captured = {}

    class Capture(FakeSMTP):
        def __init__(self, host, port, **kw):
            captured.update(host=host, port=port, **kw)

    monkeypatch.setattr(smtplib, "SMTP_SSL", Capture)
    FakeSMTP.attempts, FakeSMTP.fail_times, FakeSMTP.sent = 0, 0, []
    send_email("S", "<b>h</b>", "h", ENV, sleep=lambda s: None)
    ctx = captured["context"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert (captured["host"], captured["port"]) == ("smtp.gmail.com", 465)


def test_auth_failure_is_not_retried():
    class AuthFail(FakeSMTP):
        def login(self, user, pw):
            FakeSMTP.attempts += 1
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    FakeSMTP.attempts, FakeSMTP.sent = 0, []
    with pytest.raises(RuntimeError, match="535") as ei:
        send_email("S", "h", "h", ENV, smtp_factory=AuthFail, sleep=lambda s: pytest.fail("must not sleep"))
    assert FakeSMTP.attempts == 1 and ENV["GMAIL_APP_PASSWORD"] not in str(ei.value)


def test_5xx_rejection_is_not_retried_but_4xx_is():
    class Reject(FakeSMTP):
        code = 550

        def send_message(self, msg):  # login() (inherited) already counts the attempt
            raise smtplib.SMTPResponseException(Reject.code, b"rejected")

    Reject.code = 550
    FakeSMTP.attempts = 0
    with pytest.raises(RuntimeError, match="550"):
        send_email("S", "h", "h", ENV, smtp_factory=Reject, sleep=lambda s: None)
    assert FakeSMTP.attempts == 1
    Reject.code = 451
    FakeSMTP.attempts = 0
    with pytest.raises(RuntimeError, match="send failed after retries"):
        send_email("S", "h", "h", ENV, smtp_factory=Reject, sleep=lambda s: None)
    assert FakeSMTP.attempts == 4


def test_error_after_delivery_is_swallowed_and_never_resent():
    class QuitFails(FakeSMTP):
        def __exit__(self, *a):
            if a[0] is None:
                raise smtplib.SMTPServerDisconnected("QUIT failed")
            return False

    FakeSMTP.attempts, FakeSMTP.fail_times, FakeSMTP.sent = 0, 0, []
    send_email("S", "h", "h", ENV, smtp_factory=QuitFails, sleep=lambda s: pytest.fail("must not retry"))
    assert FakeSMTP.sent == ["S"]


def test_recipients_refused_and_sender_refused_are_permanent():
    class Refuse(FakeSMTP):
        exc = None

        def send_message(self, msg):  # login() (inherited) counts the attempt
            raise Refuse.exc

    for exc in (smtplib.SMTPRecipientsRefused({"x@y.z": (550, b"no such user")}),
                smtplib.SMTPSenderRefused(452, b"try later", "me@gmail.com")):
        Refuse.exc = exc
        FakeSMTP.attempts = 0
        with pytest.raises(RuntimeError, match="send rejected"):
            send_email("S", "h", "h", ENV, smtp_factory=Refuse, sleep=lambda s: pytest.fail("must not sleep"))
        assert FakeSMTP.attempts == 1

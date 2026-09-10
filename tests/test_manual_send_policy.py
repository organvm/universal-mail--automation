from email.message import EmailMessage

from core.send_policy import automated_send_allowed
import mail_send
import send_drafts


def test_operator_policy_forbids_programmatic_send():
    assert automated_send_allowed() is False


def test_armed_legacy_sender_never_opens_smtp(monkeypatch):
    monkeypatch.setenv("LIMEN_MAIL_SEND", "1")
    monkeypatch.setenv("LIMEN_MAIL_HOLD_SEND", "keyed_all")
    def forbidden(*args, **kwargs):
        raise AssertionError("SMTP must never be opened")
    monkeypatch.setattr(send_drafts.smtplib, "SMTP_SSL", forbidden)
    assert send_drafts.send_reply("recipient@example.invalid", "subject", "body", ("user", "secret")) is False
    assert mail_send._smtp_send(EmailMessage(), ("user", "secret")) is False


def test_manual_send_does_not_consume_old_authorization(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("manual Send must not claim a programmatic attempt")
    monkeypatch.setattr(mail_send, "claim_authorized_attempt", forbidden)
    assert mail_send.send_and_verify(EmailMessage(), ("user", "secret"), None, 0,
                                    None, "send", "old-attempt") == mail_send.EXIT_FAIL_CLOSED

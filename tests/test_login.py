"""Login safety: real IP is sent, secrets aren't logged, and rejected logins are not retried."""
import pytest

import bot.feed as feed
from bot.feed import AngelFeed, AuthError


class FakeConnect:
    instances = []

    def __init__(self, api_key=None, **kw):
        self.api_key = api_key
        self.clientPublicIp = "106.193.147.98"     # the library's hard-coded placeholder
        self.clientLocalIp = "127.0.0.1"
        FakeConnect.instances.append(self)

    def generateSession(self, client, pin, totp):
        self.sent_ip = self.clientPublicIp
        if pin != "1234":
            return {"status": False, "message": "Invalid PIN", "errorcode": "AB1050", "data": None}
        return {"status": True, "data": {"jwtToken": "Bearer abc"}}

    def getfeedToken(self):
        return "feed"


@pytest.fixture(autouse=True)
def fake_lib(monkeypatch):
    import SmartApi
    FakeConnect.instances = []
    monkeypatch.setattr(SmartApi, "SmartConnect", FakeConnect)
    monkeypatch.setenv("SERVER_PUBLIC_IP", "3.109.149.160")
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)


def test_login_sends_real_ip():
    f = AngelFeed("key", "C123", "1234", "JBSWY3DPEHPK3PXP")
    f.login()
    assert FakeConnect.instances[-1].sent_ip == "3.109.149.160"
    assert f.jwt == "abc"


def test_wrong_pin_is_not_retried():
    f = AngelFeed("key", "C123", "9999", "JBSWY3DPEHPK3PXP")
    with pytest.raises(AuthError, match="Invalid PIN AB1050"):
        f.login()
    assert len(FakeConnect.instances) == 1          # one attempt only -> no account lockout

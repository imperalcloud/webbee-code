import asyncio
from webbee.account import fetch_account, Account


class _Cfg:
    api_url = "https://auth.imperal.io"


def _run(get):
    async def _tok(): return "tok"
    return asyncio.run(fetch_account(_Cfg(), _tok, get=get))


def test_full_account():
    data = {
        "/v1/auth/me": {"imperal_id": "imp_u_1", "email": "v@imperal.io",
                         "created_at": "2026-04-27T10:00:00Z", "attributes": {"developer_tier": "explorer"}},
        "/v1/billing/subscription": {"plan": "pro", "status": "active", "expires_at": "2026-08-01T00:00:00Z"},
        "/v1/developer/profile": {"nickname": "notvallium", "tier": "explorer", "registered_at": "2026-04-27T10:00:00Z"},
    }
    async def get(path): return data[path]
    a = _run(get)
    assert a.signed_in and a.email == "v@imperal.io" and a.nickname == "notvallium"
    assert a.plan == "pro" and a.plan_status == "active"
    assert a.plan_renews == "2026-08-01"
    assert a.dev_tier == "explorer"
    assert a.member_since == "Apr 2026"


def test_partial_when_billing_and_dev_fail():
    async def get(path):
        if path == "/v1/auth/me":
            return {"email": "v@imperal.io", "created_at": "2026-04-27T10:00:00Z", "attributes": {}}
        raise RuntimeError("404")   # billing + dev unavailable
    a = _run(get)
    assert a.signed_in and a.email == "v@imperal.io"
    assert a.plan == "" and a.nickname == "" and a.member_since == "Apr 2026"


def test_signed_out_when_me_fails():
    async def get(path): raise RuntimeError("401")
    a = _run(get)
    assert a == Account(signed_in=False)


def test_signed_out_when_no_token():
    async def boom(): raise RuntimeError("no creds")
    async def get(path): return {}
    a = asyncio.run(fetch_account(_Cfg(), boom, get=get))
    assert a.signed_in is False

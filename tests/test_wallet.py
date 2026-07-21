import asyncio

from webbee.wallet import Wallet, fetch_wallet


class _Cfg:
    api_url = "https://auth.imperal.io"


def _run(get):
    async def _tok():
        return "tok"
    return asyncio.run(fetch_wallet(_Cfg(), _tok, get=get))


def test_parses_wallet_response():
    async def get(path):
        assert path == "/v1/billing/wallet"
        return {"balance": 1250, "cap": 5000, "plan": "pro",
                "status": "active", "included_tokens": 2000}
    w = _run(get)
    assert w == Wallet(balance=1250, cap=5000, plan="pro",
                       status="active", included_tokens=2000)


def test_none_on_402():
    async def get(path):
        raise RuntimeError("402 Payment Required")
    assert _run(get) is None


def test_none_on_non_200():
    async def get(path):
        raise RuntimeError("503")
    assert _run(get) is None


def test_none_when_no_token():
    async def boom():
        raise RuntimeError("no creds")
    async def get(path):
        return {"balance": 1}
    assert asyncio.run(fetch_wallet(_Cfg(), boom, get=get)) is None


def test_coerces_missing_and_bad_fields():
    async def get(path):
        return {"balance": "77", "plan": None}
    w = _run(get)
    assert w.balance == 77 and w.cap == 0 and w.plan == "" and w.status == ""

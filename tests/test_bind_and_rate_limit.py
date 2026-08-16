"""Bind guard and rate-limit parsing tests."""

from __future__ import annotations

import pytest

from app.config import assert_safe_bind, parse_rate_limit


def test_assert_safe_bind_allows_loopback() -> None:
    assert_safe_bind("127.0.0.1", allow_non_loopback=False)
    assert_safe_bind("localhost", allow_non_loopback=False)
    assert_safe_bind("::1", allow_non_loopback=False)


def test_is_loopback_host_ipv4_mapped() -> None:
    from app.config import is_loopback_host

    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("::ffff:127.0.0.1")
    assert not is_loopback_host("testclient")
    assert not is_loopback_host("8.8.8.8")
    assert not is_loopback_host(None)


def test_assert_safe_bind_refuses_lan_without_opt_in() -> None:
    with pytest.raises(RuntimeError, match="ALLOW_NON_LOOPBACK"):
        assert_safe_bind("0.0.0.0", allow_non_loopback=False)
    with pytest.raises(RuntimeError, match="ALLOW_NON_LOOPBACK"):
        assert_safe_bind("192.168.1.10", allow_non_loopback=False)


def test_assert_safe_bind_allows_opt_in() -> None:
    assert_safe_bind("0.0.0.0", allow_non_loopback=True)


def test_assert_token_for_exposure_requires_token() -> None:
    from app.config import assert_token_for_exposure

    assert_token_for_exposure(allow_non_loopback=False, api_token=None)
    assert_token_for_exposure(allow_non_loopback=True, api_token="secret")
    assert_token_for_exposure(
        allow_non_loopback=True, api_token=None, supabase_auth=True
    )
    with pytest.raises(RuntimeError, match="JARVIS_API_TOKEN|Supabase"):
        assert_token_for_exposure(allow_non_loopback=True, api_token=None)
    with pytest.raises(RuntimeError, match="UNAUTHENTICATED"):
        assert_token_for_exposure(
            allow_non_loopback=True,
            api_token="secret",
            allow_unauthenticated_api=True,
        )


def test_parse_rate_limit() -> None:
    assert parse_rate_limit("20/minute") == (20, 60.0)
    assert parse_rate_limit("5/second") == (5, 1.0)
    with pytest.raises(ValueError):
        parse_rate_limit("plenty")

"""Policy enforcement tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.security import Decision, PolicyEngine, sanitize_filename


def engine() -> PolicyEngine:
    return PolicyEngine()


def test_unlisted_tool_is_denied() -> None:
    verdict = engine().check("shell_exec")
    assert verdict.decision is Decision.DENY
    assert verdict.code == "tool_not_allowlisted"


def test_allowlisted_shell_still_requires_capability() -> None:
    """allow_shell:false must block shell_exec even if someone allowlists it."""
    from app.security import Policy

    base = engine().policy.model_dump()
    base["allowed_tools"] = list(base["allowed_tools"]) + ["shell_exec"]
    base["allow_shell"] = False
    policy = PolicyEngine(Policy.model_validate(base))
    verdict = policy.check("shell_exec", mode="shell")
    assert verdict.decision is Decision.DENY
    assert verdict.code == "allow_shell_disabled"


def test_allowlisted_web_search_requires_network_capability() -> None:
    """allow_network:false must block web_search even when allowlisted."""
    from app.security import Policy

    base = engine().policy.model_dump()
    base["allow_network"] = False
    # web_search is already on the default allowlist in config/rules.md.
    policy = PolicyEngine(Policy.model_validate(base))
    verdict = policy.check("web_search", mode="network")
    assert verdict.decision is Decision.DENY
    assert verdict.code == "allow_network_disabled"


def test_delete_disabled_by_default() -> None:
    verdict = engine().check("vault_write", path="./data/note.md", mode="delete")
    assert verdict.decision is Decision.DENY
    assert verdict.code == "delete_disabled"


def test_path_outside_sandbox_is_denied() -> None:
    verdict = engine().check(
        "vault_read", path="C:/Windows/System32/drivers/etc/hosts", mode="read"
    )
    assert verdict.decision is Decision.DENY
    assert verdict.code == "path_outside_sandbox"


def test_traversal_cannot_escape_an_allowed_root() -> None:
    verdict = engine().check("vault_read", path="./data/../../../secrets.txt", mode="read")
    assert verdict.decision is Decision.DENY


def test_denied_path_beats_allowed_root() -> None:
    verdict = engine().check("vault_read", path="./config/rules.md", mode="read")
    assert verdict.decision is Decision.DENY
    assert verdict.code == "path_denied"


def test_read_inside_sandbox_is_allowed() -> None:
    assert engine().check("vault_read", path="./data/notes/a.md", mode="read").allowed


def test_write_requires_approval_then_grant_consumes_it() -> None:
    policy = engine()
    first = policy.check("vault_write", path="./data/a.md", mode="write")
    assert first.decision is Decision.REQUIRE_APPROVAL

    request_id = policy.mint_approval(
        "vault_write", path="./data/a.md", mode="write"
    )
    assert policy.resolve_approval(request_id, approved=True, tool="vault_write")
    granted = policy.check(
        "vault_write", path="./data/a.md", mode="write", approval_id=request_id
    )
    assert granted.allowed

    reused = policy.check(
        "vault_write", path="./data/a.md", mode="write", approval_id=request_id
    )
    assert reused.decision is Decision.REQUIRE_APPROVAL, "a grant must be single use"


def test_forged_approval_id_cannot_create_grant() -> None:
    """Client-invented ids must not become grants without a minted pending."""
    policy = engine()
    assert not policy.resolve_approval("forged-id", approved=True, tool="vault_write")
    denied = policy.check(
        "vault_write", path="./data/a.md", mode="write", approval_id="forged-id"
    )
    assert denied.decision is Decision.REQUIRE_APPROVAL


def test_grant_is_bound_to_path() -> None:
    """A grant minted for one path cannot authorize a different path."""
    policy = engine()
    request_id = policy.mint_approval("vault_write", path="./data/a.md", mode="write")
    assert policy.resolve_approval(request_id, approved=True)
    wrong = policy.check(
        "vault_write", path="./data/b.md", mode="write", approval_id=request_id
    )
    assert wrong.decision is Decision.REQUIRE_APPROVAL
    ok = policy.check(
        "vault_write", path="./data/a.md", mode="write", approval_id=request_id
    )
    assert ok.allowed


def test_deny_clears_pending_without_grant() -> None:
    """Deny must remove the pending request and leave no consumable grant."""
    policy = engine()
    request_id = policy.mint_approval("vault_write", path="./data/a.md", mode="write")
    assert policy.resolve_approval(request_id, approved=False, tool="vault_write")
    assert not policy.resolve_approval(request_id, approved=True, tool="vault_write")
    denied = policy.check(
        "vault_write", path="./data/a.md", mode="write", approval_id=request_id
    )
    assert denied.decision is Decision.REQUIRE_APPROVAL


def test_turn_approval_id_is_consumed_on_check() -> None:
    """Chat attaches approval_id via begin_turn without client-side forging."""
    policy = engine()
    request_id = policy.mint_approval("vault_write", path="./data/a.md", mode="write")
    assert policy.resolve_approval(request_id, approved=True)
    turn = policy.begin_turn(request_id)
    allowed = policy.check("vault_write", path="./data/a.md", mode="write", turn=turn)
    assert allowed.allowed
    turn2 = policy.begin_turn(request_id)
    again = policy.check("vault_write", path="./data/a.md", mode="write", turn=turn2)
    assert again.decision is Decision.REQUIRE_APPROVAL


def test_tool_call_budget_is_enforced() -> None:
    policy = engine()
    turn = policy.begin_turn()
    limit = policy.policy.max_tool_calls_per_turn
    for _ in range(limit):
        policy.check("vault_read", path="./data/a.md", mode="read", turn=turn)
    assert (
        policy.check("vault_read", path="./data/a.md", mode="read", turn=turn).code
        == "tool_call_budget"
    )

    refreshed = policy.begin_turn()
    assert policy.check("vault_read", path="./data/a.md", mode="read", turn=refreshed).allowed


def test_concurrent_turns_do_not_share_budgets() -> None:
    """Two overlapping turns must not deduct from each other's ceilings."""
    policy = engine()
    t1 = policy.begin_turn()
    t2 = policy.begin_turn()
    limit = policy.policy.max_tool_calls_per_turn
    for _ in range(limit):
        policy.check("vault_read", path="./data/a.md", mode="read", turn=t1)
    assert (
        policy.check("vault_read", path="./data/a.md", mode="read", turn=t1).code
        == "tool_call_budget"
    )
    assert policy.check("vault_read", path="./data/a.md", mode="read", turn=t2).allowed
    assert t1.budget.tool_calls == limit + 1
    assert t2.budget.tool_calls == 1


def test_concurrent_turns_do_not_share_approval_ids() -> None:
    """A grant attached to one turn must not authorize the other."""
    policy = engine()
    request_id = policy.mint_approval("vault_write", path="./data/a.md", mode="write")
    assert policy.resolve_approval(request_id, approved=True)
    granted_turn = policy.begin_turn(request_id)
    other = policy.begin_turn()
    assert not policy.check(
        "vault_write", path="./data/a.md", mode="write", turn=other
    ).allowed
    assert policy.check(
        "vault_write", path="./data/a.md", mode="write", turn=granted_turn
    ).allowed


def test_download_disabled_by_default() -> None:
    """allow_download:false must block quarantine destinations."""
    from app.security import PolicyDenied

    with pytest.raises(PolicyDenied) as exc:
        engine().quarantine_target("payload.exe")
    assert exc.value.verdict.code == "allow_download_disabled"


def test_download_target_requires_capability_and_lands_in_quarantine() -> None:
    """With allow_download, quarantine_target checks policy and strips executables."""
    from app.security import Policy

    base = engine().policy.model_dump()
    base["allow_download"] = True
    if "file_download" not in base["allowed_tools"]:
        base["allowed_tools"] = list(base["allowed_tools"]) + ["file_download"]
    # file_download is in require_approval_for by default — mint a grant.
    policy = PolicyEngine(Policy.model_validate(base))
    request_id = policy.mint_approval(
        "file_download",
        path=policy.quarantine_dir / "payload.exe.download",
        mode="download",
    )
    assert policy.resolve_approval(request_id, approved=True, tool="file_download")
    target = policy.quarantine_target(
        "../../payload.exe", approval_id=request_id
    )
    assert target.name == "payload.exe.download"
    assert target.parent == policy.quarantine_dir


def test_approval_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pending approvals older than APPROVAL_TTL_SECONDS cannot be granted."""
    import time

    from app import security as security_mod

    policy = engine()
    request_id = policy.mint_approval("vault_write", path="./data/a.md", mode="write")
    # Force the pending record into the past beyond TTL.
    with policy._lock:
        pending = policy._pending[request_id]
        policy._pending[request_id] = security_mod._PendingApproval(
            tool=pending.tool,
            path=pending.path,
            mode=pending.mode,
            created_mono=time.monotonic() - security_mod.APPROVAL_TTL_SECONDS - 1,
        )
    assert not policy.resolve_approval(request_id, approved=True, tool="vault_write")


def test_pending_approvals_are_capped() -> None:
    """Minting beyond MAX_PENDING_APPROVALS drops the oldest entries."""
    from app import security as security_mod

    policy = engine()
    ids: list[str] = []
    for i in range(security_mod.MAX_PENDING_APPROVALS + 5):
        ids.append(
            policy.mint_approval("vault_write", path=f"./data/{i}.md", mode="write")
        )
    with policy._lock:
        assert len(policy._pending) == security_mod.MAX_PENDING_APPROVALS
        assert ids[0] not in policy._pending


def test_policy_elevation_reasons_detect_capability_and_sandbox() -> None:
    """Enabling shell is elevation; turning it back off is not."""
    from app.security import policy_elevation_reasons

    previous = engine().policy
    elevated = previous.model_copy(update={"allow_shell": True})
    reasons = policy_elevation_reasons(previous, elevated)
    assert any("allow_shell" in r for r in reasons)
    assert policy_elevation_reasons(elevated, previous) == []


def test_sanitize_filename() -> None:
    assert sanitize_filename("a/b/../c.md") == "c.md"
    assert sanitize_filename("") == "download"
    assert sanitize_filename("con.txt").startswith("_")
    assert "\x00" not in sanitize_filename("bad\x00name")


def test_is_within_rejects_prefix_sibling(tmp_path: Path) -> None:
    """Prefix paths must not pass containment (unlike str.startswith)."""
    from app.security import is_within

    parent = tmp_path / "vault"
    parent.mkdir()
    sibling = tmp_path / "vault_evil"
    sibling.mkdir()
    secret = sibling / "secret.png"
    secret.write_bytes(b"x")
    child = parent / "note.png"
    child.write_bytes(b"x")
    assert not is_within(secret.resolve(), parent.resolve())
    assert is_within(child.resolve(), parent.resolve())


def test_resolve_image_path_rejects_prefix_sibling(tmp_path: Path) -> None:
    """OCR must not follow images outside the vault via startswith false-allow."""
    from app.ingestion.ocr import resolve_image_path

    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "Note.md"
    note.write_text("x", encoding="utf-8")
    evil = tmp_path / "vault_evil"
    evil.mkdir()
    (evil / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    # Relative escape that resolves beside the vault with a shared prefix.
    assert resolve_image_path(vault, note, "../vault_evil/shot.png") is None
    inside = vault / "shot.png"
    inside.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert resolve_image_path(vault, note, "shot.png") == inside.resolve()

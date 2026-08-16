"""Policy enforcement.

Parses the YAML frontmatter of ``config/rules.md`` and enforces it in code. The
markdown body of that file is prompt text and carries no authority: a model that
ignores it still cannot get a denied call past :class:`PolicyEngine`.

Every filesystem-touching tool must route through :meth:`PolicyEngine.check`.
Nothing else in the codebase should open a path the user supplied.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.config import PROJECT_ROOT, get_settings

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

# Pending approvals and one-shot grants expire; caps bound memory under load.
APPROVAL_TTL_SECONDS = 300.0
MAX_PENDING_APPROVALS = 64
MAX_GRANTS = 64

# Capabilities / tools that require an explicit confirm_elevation on PUT /api/rules.
_ELEVATION_CAPABILITIES = (
    "allow_delete",
    "allow_download",
    "allow_shell",
    "allow_email_send",
)
_ELEVATION_TOOLS = frozenset({"shell_exec", "email_send", "file_download"})

PolicyMode = Literal[
    "read", "write", "delete", "download", "shell", "network", "email_send"
]

# Suffixes that Windows or a shell may execute on double-click. Downloads keep
# their name but gain a trailing .download so nothing is directly runnable.
EXECUTABLE_SUFFIXES = frozenset(
    {
        ".exe", ".dll", ".com", ".scr", ".msi", ".msp", ".cpl", ".jar",
        ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse",
        ".wsf", ".wsh", ".hta", ".reg", ".lnk", ".inf", ".sh", ".bash",
    }
)

# Reserved device names on Windows; creating these does not do what you expect.
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyDenied(Exception):
    """Raised when a tool call is refused outright."""

    def __init__(self, verdict: PolicyVerdict) -> None:
        """Attach the full verdict so callers can surface code and details."""
        super().__init__(verdict.reason)
        self.verdict = verdict


class ApprovalRequired(Exception):
    """Raised when a tool call is permitted but needs a human to confirm it."""

    def __init__(self, verdict: PolicyVerdict) -> None:
        """Attach the full verdict so the UI can prompt for confirmation."""
        super().__init__(verdict.reason)
        self.verdict = verdict


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    decision: Decision
    tool: str
    reason: str
    code: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        """True only for an unconditional allow (not pending approval)."""
        return self.decision is Decision.ALLOW

    def raise_for_decision(self) -> None:
        """Raise :class:`PolicyDenied` or :class:`ApprovalRequired` when not allowed."""
        if self.decision is Decision.DENY:
            raise PolicyDenied(self)
        if self.decision is Decision.REQUIRE_APPROVAL:
            raise ApprovalRequired(self)


class Policy(BaseModel):
    """The machine-enforced half of ``config/rules.md``."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1

    allow_delete: bool = False
    allow_download: bool = False
    allow_shell: bool = False
    allow_network: bool = True
    allow_email_send: bool = False
    allow_vault_write: bool = True

    vault_path: str = ""
    allowed_read_paths: list[str] = Field(default_factory=list)
    allowed_write_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)

    trash_dir: str = "./data/trash"
    quarantine_dir: str = "./data/quarantine"

    require_approval_for: list[str] = Field(default_factory=list)

    max_file_writes_per_turn: int = 5
    max_tool_calls_per_turn: int = 25
    max_download_bytes: int = 25 * 1024 * 1024

    allowed_tools: list[str] = Field(default_factory=list)

    # Populated from the markdown body, not the frontmatter.
    prompt_text: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> Policy:
        """Parse YAML frontmatter into fields and keep the markdown body as prompt text."""
        path = path or get_settings().rules_path
        raw = path.read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(raw)
        if not match:
            msg = f"{path} is missing its YAML frontmatter policy block"
            raise ValueError(msg)

        data = yaml.safe_load(match.group(1)) or {}
        if not isinstance(data, dict):
            msg = f"{path} frontmatter must be a mapping"
            raise TypeError(msg)

        data["prompt_text"] = match.group(2).strip()
        return cls.model_validate(data)

    def dump(self, path: Path | None = None) -> None:
        """Write the policy back, preserving the prose body."""
        path = path or get_settings().rules_path
        frontmatter = self.model_dump(exclude={"prompt_text"})
        body = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
        path.write_text(
            f"---\n{body}---\n\n{self.prompt_text}\n", encoding="utf-8"
        )


def _expand(template: str, vault_path: str) -> str:
    """Substitute ``${vault_path}`` in configured path templates."""
    return template.replace("${vault_path}", vault_path)


def _abs(raw: str) -> Path:
    """Resolve a configured path against the project root."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return _resolve_unsafe(p)


def _resolve_unsafe(p: Path) -> Path:
    """Resolve symlinks even for a path that does not exist yet.

    ``Path.resolve()`` on a missing path leaves the missing tail unresolved but
    still resolves existing ancestors, which is what matters: a symlink in the
    existing part of the path cannot be used to escape a sandbox check.
    """
    return Path(os.path.abspath(p)).resolve()


def is_within(child: Path, parent: Path) -> bool:
    """Containment test that is correct on case-insensitive filesystems."""
    c = os.path.normcase(str(child))
    p = os.path.normcase(str(parent))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def sanitize_filename(name: str, fallback: str = "download") -> str:
    """Reduce an arbitrary string to a safe single path segment."""
    name = unicodedata.normalize("NFKC", name)
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .")
    if not name or name in {".", ".."}:
        return fallback
    if Path(name).stem.lower() in WINDOWS_RESERVED:
        name = f"_{name}"
    return name[:180]


@dataclass
class TurnBudget:
    """Per-turn call ceilings, so a loop cannot grind through the vault."""

    tool_calls: int = 0
    file_writes: int = 0

    def reset(self) -> None:
        """Clear per-turn counters before a new chat request."""
        self.tool_calls = 0
        self.file_writes = 0


@dataclass
class TurnState:
    """Per-request budget and optional approval grant id.

    Concurrent chat turns each hold their own ``TurnState`` so budgets and
    approval ids never cross. Shared pending/grant maps stay on the engine
    under ``_lock``; this object is never protected by that lock.
    """

    budget: TurnBudget = field(default_factory=TurnBudget)
    approval_id: str | None = None


# Ambient turn for nested ``check`` calls during a chat stream (explicit
# ``turn=`` still preferred when the caller has it).
_current_turn: ContextVar[TurnState | None] = ContextVar(
    "jarvis_policy_turn", default=None
)


def activate_turn(turn: TurnState) -> Token[TurnState | None]:
    """Bind ``turn`` for nested policy checks in this async/task context."""
    return _current_turn.set(turn)


def reset_turn(token: Token[TurnState | None]) -> None:
    """Restore the previous ambient turn after a chat stream finishes."""
    _current_turn.reset(token)


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    """Server-minted approval request awaiting allow/deny."""

    tool: str
    path: str | None
    mode: str | None
    created_mono: float


@dataclass(frozen=True, slots=True)
class _ApprovalGrant:
    """One-shot grant bound to the tool (and path) that was approved."""

    tool: str
    path: str | None
    created_mono: float


def policy_elevation_reasons(previous: Policy, proposed: Policy) -> list[str]:
    """Return human-readable reasons the proposed policy elevates privileges.

    Empty list means the change is a narrowing or same-privilege edit.
    """
    reasons: list[str] = []
    for cap in _ELEVATION_CAPABILITIES:
        if not getattr(previous, cap) and getattr(proposed, cap):
            reasons.append(f"enables {cap}")

    prev_tools = set(previous.allowed_tools)
    for tool in proposed.allowed_tools:
        if tool not in prev_tools and tool in _ELEVATION_TOOLS:
            reasons.append(f"adds high-risk tool {tool}")

    prev_vault = previous.vault_path.strip()
    new_vault = proposed.vault_path.strip()
    if new_vault and new_vault != prev_vault:
        reasons.append("changes vault_path")

    def _roots(policy: Policy, templates: list[str]) -> list[Path]:
        vault = policy.vault_path.strip()
        return [
            _abs(_expand(r, vault))
            for r in templates
            if _expand(r, vault).strip()
        ]

    prev_read = _roots(previous, previous.allowed_read_paths)
    prev_write = _roots(previous, previous.allowed_write_paths)
    for root in _roots(proposed, proposed.allowed_read_paths):
        if prev_read and not any(is_within(root, old) for old in prev_read):
            reasons.append(f"expands read sandbox to {root}")
            break
    for root in _roots(proposed, proposed.allowed_write_paths):
        if prev_write and not any(is_within(root, old) for old in prev_write):
            reasons.append(f"expands write sandbox to {root}")
            break

    return reasons


class PolicyEngine:
    """Single choke point for every privileged action.

    Policy text and sandbox roots are shared. Pending approvals and grants are
    shared under one ``threading.Lock``. Per-turn budgets live on
    :class:`TurnState` only — never as mutation of a singleton budget field.
    """

    def __init__(self, policy: Policy | None = None) -> None:
        """Load policy from disk when omitted and resolve sandbox roots."""
        self._policy = policy or Policy.load()
        self._pending: dict[str, _PendingApproval] = {}
        self._grants: dict[str, _ApprovalGrant] = {}
        self._lock = threading.Lock()
        self._refresh()

    # -- lifecycle ---------------------------------------------------------

    @property
    def policy(self) -> Policy:
        """Current in-memory policy (not re-read until :meth:`reload`)."""
        return self._policy

    def reload(self) -> Policy:
        """Re-read ``rules.md`` from disk and rebuild path roots."""
        self._policy = Policy.load()
        self._refresh()
        return self._policy

    def _refresh(self) -> None:
        """Resolve vault, allowlists, and trash/quarantine paths from policy fields."""
        p = self._policy
        vault = p.vault_path.strip()
        self.vault_path = _abs(vault) if vault else None
        self._read_roots = [_abs(_expand(r, vault)) for r in p.allowed_read_paths if _expand(r, vault).strip()]
        self._write_roots = [_abs(_expand(r, vault)) for r in p.allowed_write_paths if _expand(r, vault).strip()]
        self._denied = [_abs(_expand(r, vault)) for r in p.denied_paths if _expand(r, vault).strip()]
        self.trash_dir = _abs(p.trash_dir)
        self.quarantine_dir = _abs(p.quarantine_dir)

    def begin_turn(self, approval_id: str | None = None) -> TurnState:
        """Return a fresh per-request turn; does not mutate shared engine state."""
        return TurnState(budget=TurnBudget(), approval_id=approval_id)

    def system_prompt(self) -> str:
        """Markdown body of ``rules.md`` to inject into the model system prompt."""
        return self._policy.prompt_text

    def _resolve_turn(self, turn: TurnState | None) -> TurnState:
        """Prefer explicit turn, then ambient context, else a one-shot ephemeral."""
        if turn is not None:
            return turn
        ambient = _current_turn.get()
        if ambient is not None:
            return ambient
        return TurnState()

    # -- approvals ---------------------------------------------------------

    def mint_approval(
        self,
        tool: str,
        *,
        path: str | Path | None = None,
        mode: str | None = None,
    ) -> str:
        """Record a pending approval bound to ``tool``/``path``; return its id."""
        request_id = uuid.uuid4().hex
        bound_path = (
            str(_resolve_unsafe(Path(path).expanduser())) if path is not None else None
        )
        now = time.monotonic()
        with self._lock:
            self._prune_approvals_locked(now)
            self._pending[request_id] = _PendingApproval(
                tool=tool, path=bound_path, mode=mode, created_mono=now
            )
            self._cap_pending_locked()
        return request_id

    def resolve_approval(
        self,
        request_id: str,
        *,
        approved: bool,
        tool: str | None = None,
    ) -> bool:
        """Allow or deny a server-minted pending request.

        Client-supplied ``tool`` is validated against the pending record when
        present; the grant always uses the server-bound tool and path. Deny
        clears the pending entry without creating a grant. Returns False when
        ``request_id`` is unknown, expired, or the tool mismatches.
        """
        now = time.monotonic()
        with self._lock:
            self._prune_approvals_locked(now)
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            if tool is not None and tool != pending.tool:
                return False
            del self._pending[request_id]
            if approved:
                self._grants[request_id] = _ApprovalGrant(
                    tool=pending.tool,
                    path=pending.path,
                    created_mono=now,
                )
                self._cap_grants_locked()
            return True

    def _prune_approvals_locked(self, now: float | None = None) -> None:
        """Drop expired pending approvals and grants. Caller must hold ``_lock``."""
        mono = time.monotonic() if now is None else now
        expired_pending = [
            key
            for key, item in self._pending.items()
            if mono - item.created_mono > APPROVAL_TTL_SECONDS
        ]
        for key in expired_pending:
            del self._pending[key]
        expired_grants = [
            key
            for key, item in self._grants.items()
            if mono - item.created_mono > APPROVAL_TTL_SECONDS
        ]
        for key in expired_grants:
            del self._grants[key]

    def _cap_pending_locked(self) -> None:
        """Bound pending map size by dropping oldest entries."""
        while len(self._pending) > MAX_PENDING_APPROVALS:
            oldest = min(self._pending.items(), key=lambda kv: kv[1].created_mono)
            del self._pending[oldest[0]]

    def _cap_grants_locked(self) -> None:
        """Bound grant map size by dropping oldest entries."""
        while len(self._grants) > MAX_GRANTS:
            oldest = min(self._grants.items(), key=lambda kv: kv[1].created_mono)
            del self._grants[oldest[0]]

    def _consume_grant(
        self,
        request_id: str | None,
        tool: str,
        *,
        path: str | Path | None = None,
    ) -> bool:
        """Spend a one-shot grant if tool (and path, when bound) match.

        Caller must hold ``self._lock``.
        """
        if not request_id:
            return False
        self._prune_approvals_locked()
        grant = self._grants.get(request_id)
        if grant is None or grant.tool != tool:
            return False
        if grant.path is not None:
            if path is None:
                return False
            actual = str(_resolve_unsafe(Path(path).expanduser()))
            if actual != grant.path:
                return False
        del self._grants[request_id]
        return True

    # -- checks ------------------------------------------------------------

    def check(
        self,
        tool: str,
        *,
        path: str | Path | None = None,
        mode: PolicyMode = "read",
        size: int | None = None,
        approval_id: str | None = None,
        turn: TurnState | None = None,
    ) -> PolicyVerdict:
        """Evaluate a tool call against ``turn`` (or ambient/ephemeral budget).

        Does not hold ``_lock`` across path I/O — only around grant consume.
        Callers must act on the verdict.
        """
        p = self._policy
        state = self._resolve_turn(turn)

        if tool not in p.allowed_tools:
            return self._deny(tool, "tool_not_allowlisted",
                              f"'{tool}' is not in allowed_tools.")

        tool_capability = _CAPABILITY_BY_TOOL.get(tool)
        if tool_capability and not getattr(p, tool_capability):
            return self._deny(
                tool,
                f"{tool_capability}_disabled",
                f"'{tool}' is disabled by policy ({tool_capability}: false).",
            )

        state.budget.tool_calls += 1
        if state.budget.tool_calls > p.max_tool_calls_per_turn:
            return self._deny(tool, "tool_call_budget",
                              f"Exceeded {p.max_tool_calls_per_turn} tool calls this turn.")

        capability = _CAPABILITY_BY_MODE.get(mode)
        if capability and not getattr(p, capability):
            return self._deny(tool, f"{mode}_disabled",
                              f"{mode.capitalize()} is disabled by policy ({capability}: false).")

        if mode == "write":
            state.budget.file_writes += 1
            if state.budget.file_writes > p.max_file_writes_per_turn:
                return self._deny(tool, "write_budget",
                                  f"Exceeded {p.max_file_writes_per_turn} file writes this turn.")

        if size is not None and size > p.max_download_bytes:
            return self._deny(tool, "size_limit",
                              f"{size} bytes exceeds the {p.max_download_bytes} byte limit.")

        if path is not None:
            verdict = self._check_path(tool, path, mode)
            if not verdict.allowed:
                return verdict

        action = _ACTION_BY_MODE.get(mode, tool)
        if action in p.require_approval_for or tool in p.require_approval_for:
            aid = approval_id if approval_id is not None else state.approval_id
            with self._lock:
                consumed = self._consume_grant(aid, tool, path=path)
            if not consumed:
                return PolicyVerdict(
                    Decision.REQUIRE_APPROVAL, tool,
                    f"'{action}' requires your confirmation.",
                    "approval_required",
                    {"path": str(path) if path else None, "mode": mode},
                )

        return PolicyVerdict(Decision.ALLOW, tool, "ok")

    def _check_path(
        self, tool: str, path: str | Path, mode: str
    ) -> PolicyVerdict:
        """Deny paths under denied roots or outside the mode's sandbox allowlist."""
        target = _resolve_unsafe(Path(path).expanduser())

        for denied in self._denied:
            if is_within(target, denied):
                return self._deny(tool, "path_denied",
                                  f"{target} is inside a denied path ({denied}).",
                                  {"path": str(target)})

        if mode == "download":
            # Downloads may only land under the configured quarantine directory.
            if not is_within(target, self.quarantine_dir):
                return self._deny(
                    tool,
                    "path_outside_sandbox",
                    f"{target} is outside the quarantine directory ({self.quarantine_dir}).",
                    {"path": str(target), "roots": [str(self.quarantine_dir)]},
                )
            return PolicyVerdict(Decision.ALLOW, tool, "ok")

        roots = self._write_roots if mode in {"write", "delete"} else self._read_roots
        if not roots:
            return self._deny(tool, "no_roots_configured",
                              "No allowed paths are configured. Set vault_path in config/rules.md.")

        if not any(is_within(target, root) for root in roots):
            return self._deny(tool, "path_outside_sandbox",
                              f"{target} is outside every allowed {mode} path.",
                              {"path": str(target), "roots": [str(r) for r in roots]})

        return PolicyVerdict(Decision.ALLOW, tool, "ok")

    @staticmethod
    def _deny(tool: str, code: str, reason: str,
              details: dict[str, Any] | None = None) -> PolicyVerdict:
        """Build a deny verdict with a stable machine-readable ``code``."""
        return PolicyVerdict(Decision.DENY, tool, reason, code, details or {})

    # -- privileged operations --------------------------------------------

    def trash(
        self,
        path: str | Path,
        *,
        approval_id: str | None = None,
        turn: TurnState | None = None,
    ) -> Path:
        """Move a file to the trash directory. Deletion is never destructive."""
        self.check(
            "vault_write",
            path=path,
            mode="delete",
            approval_id=approval_id,
            turn=turn,
        ).raise_for_decision()

        source = _resolve_unsafe(Path(path).expanduser())
        if not source.exists():
            msg = f"{source} does not exist"
            raise FileNotFoundError(msg)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        destination = self.trash_dir / f"{stamp}-{sanitize_filename(source.name)}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        return destination

    def assert_operator_vault_write(self, path: str | Path) -> None:
        """Allow a human-initiated vault write after sandbox checks only.

        Skips the agent approval gate and per-turn write budget — the UI action
        is the confirmation. Still requires ``allow_vault_write`` and path roots.
        """
        p = self._policy
        if not p.allow_vault_write:
            self._deny(
                "vault_write",
                "allow_vault_write_disabled",
                "Vault writes are disabled by policy (allow_vault_write: false).",
            ).raise_for_decision()
        if "vault_write" not in p.allowed_tools:
            self._deny(
                "vault_write",
                "tool_not_allowlisted",
                "'vault_write' is not in allowed_tools.",
            ).raise_for_decision()
        verdict = self._check_path("vault_write", path, "write")
        verdict.raise_for_decision()

    def operator_trash_vault_file(self, path: str | Path) -> Path:
        """Move a vault file to trash for UI-initiated ingest cleanup.

        Skips the agent approval gate and ``allow_delete`` — the operator action
        is the confirmation. Still requires ``allow_vault_write`` and sandbox roots.
        """
        self.assert_operator_vault_write(path)
        source = _resolve_unsafe(Path(path).expanduser())
        if not source.exists():
            msg = f"{source} does not exist"
            raise FileNotFoundError(msg)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        destination = self.trash_dir / f"{stamp}-{sanitize_filename(source.name)}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return destination

    def quarantine_target(
        self,
        filename: str,
        *,
        approval_id: str | None = None,
        turn: TurnState | None = None,
        size: int | None = None,
    ) -> Path:
        """Where a download is allowed to land, with the run bit taken off.

        Requires ``file_download`` on the tool allowlist and ``allow_download``.
        """
        safe = sanitize_filename(filename)
        if Path(safe).suffix.lower() in EXECUTABLE_SUFFIXES:
            safe = f"{safe}.download"
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = self.quarantine_dir / safe
        self.check(
            "file_download",
            path=target,
            mode="download",
            size=size,
            approval_id=approval_id,
            turn=turn,
        ).raise_for_decision()
        return target


_CAPABILITY_BY_MODE = {
    "write": "allow_vault_write",
    "delete": "allow_delete",
    "download": "allow_download",
    "shell": "allow_shell",
    "network": "allow_network",
    "email_send": "allow_email_send",
}

# Tools that require a capability switch even when allowlisted.
_CAPABILITY_BY_TOOL = {
    "shell_exec": "allow_shell",
    "web_search": "allow_network",
    "email_send": "allow_email_send",
    "file_download": "allow_download",
}

_ACTION_BY_MODE = {
    "write": "file_write",
    "delete": "file_delete",
    "read": "file_read",
    "download": "file_download",
    "shell": "shell_exec",
    "network": "web_search",
    "email_send": "email_send",
}


_engine: PolicyEngine | None = None


def get_policy_engine() -> PolicyEngine:
    """Return the process-wide policy engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = PolicyEngine()
    return _engine


def reset_policy_engine() -> None:
    """Drop the singleton so the next call reloads from current settings."""
    global _engine
    _engine = None

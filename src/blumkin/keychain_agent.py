"""The keychain, reached through ``blumkin-agent`` instead of straight from Python (#773).

A keychain item's access list names binaries, and an ad-hoc signed Python is a new binary after
every update, so each process that read the keychain itself raised its own macOS dialog. With the
agent as the only client, the identity macOS sees is the agent's, which changes only when blumkin
is upgraded; and a process that holds no keychain access of its own (an MCP server, a loop run)
simply asks the agent.

``AgentKeyring`` is ``keyring``-shaped (``get_password`` / ``set_password`` / ``delete_password``),
so the one-item vault in ``keychain_vault`` sits on top of it unchanged. It goes through the
agent whenever there is one, and falls back to the direct keyring, behind the authorization gate,
only when the agent cannot be reached or started (no binary built in a checkout, an unsupported
platform).

Every agent request is presence-gated on the agent's side: a recent Touch ID or device-password
check lets it through, otherwise the agent asks. That is the agent's policy and not this module's.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

from blumkin.agent import client as agent_client
from blumkin.agent.paths import binary_path

SERVICE = "blumkin"
DISABLE_ENV = "BLUMKIN_KEYCHAIN_VIA_AGENT"

_PRESENCE_ERRORS = frozenset(
    {"presence_busy", "presence_denied", "presence_timed_out", "presence_unsupported"}
)


class KeychainAgentError(OSError):
    """The agent refused or failed a keychain request (a declined prompt, a keychain error)."""


class KeychainAgentTimeoutError(KeychainAgentError, TimeoutError):
    """The agent took the request and did not answer in time.

    A ``TimeoutError`` on purpose: the secret store treats one as "the write may still land
    later" and refuses to follow it with a fallback write (issue #287), exactly as for a keychain
    call that timed out.
    """


class _Errors:
    """The slice of ``keyring.errors`` the secret store looks at."""

    class PasswordDeleteError(Exception):
        """Nothing to delete (mirrors ``keyring.errors.PasswordDeleteError``)."""


class AgentKeyring:
    """``keyring``-shaped access to blumkin's keychain item through the agent."""

    def __init__(self, direct: Any, *, bound: Callable[..., Any] | None = None) -> None:
        self._direct = direct
        # Bounds a direct keyring call (the secret store passes its short keychain budget): the
        # call that falls back runs inside the long agent budget, and must not inherit it.
        self._bound = bound

    def __getattr__(self, name: str) -> Any:
        return getattr(self._direct, name)

    @property
    def errors(self) -> Any:
        """The real ``keyring.errors`` when there is one, so a not-found delete is recognized."""
        return getattr(self._direct, "errors", None) or _Errors

    def delete_password(self, service: str, account: str) -> None:
        reply = self._request("keychain_delete", service, account)
        if reply is None:
            self._allow_direct()
            self._run_direct(self._direct.delete_password, service, account)
            return
        if not reply.get("deleted"):
            raise self.errors.PasswordDeleteError(account)

    def get_password(self, service: str, account: str) -> str | None:
        reply = self._request("keychain_get", service, account)
        if reply is None:
            self._allow_direct()
            return self._run_direct(self._direct.get_password, service, account)
        if not reply.get("found"):
            return None
        value = reply.get("value")
        return value if isinstance(value, str) else None

    def set_password(self, service: str, account: str, password: str) -> None:
        reply = self._request("keychain_set", service, account, {"value": password})
        if reply is None:
            self._allow_direct()
            self._run_direct(self._direct.set_password, service, account, password)

    def _run_direct(self, func: Callable[..., Any], *args: Any) -> Any:
        return self._bound(func, *args) if self._bound is not None else func(*args)

    def _allow_direct(self) -> None:
        """The agent is out of reach: a direct keychain call needs the authorization gate."""
        from blumkin import keychain_gate  # noqa: PLC0415 - the gate imports this module

        global _fell_back_ever
        keychain_gate.require(direct=True)
        _fell_back_ever = True

    def _request(
        self, cmd: str, service: str, account: str, extra: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """The agent's successful reply, or None when there is no agent to ask.

        A refusal or failure from a running agent raises ``KeychainAgentError``: falling back to
        the direct keyring after a declined prompt would be asking the same question another way.
        """
        if service != SERVICE:
            raise KeychainAgentError(f"the agent serves only the {SERVICE!r} keychain service")
        request = {"account": account, "service": service} | (extra or {})
        try:
            reply = agent_client.call(cmd, extra=request)
        except (agent_client.AgentUnreachableError, agent_client.AgentReplyLostError) as exc:
            # An agent answered the connection but not in time: a prompt may still be waiting on
            # its side. Reading the keychain directly now would raise a second dialog.
            raise KeychainAgentTimeoutError(
                "the agent did not answer in time (a Touch ID or keychain prompt may be waiting, "
                "or the request may already have been handled); not asking the keychain directly"
            ) from exc
        except agent_client.AgentUnavailableError:
            return None
        if reply.get("ok") is True:
            return reply
        error = str(reply.get("error", "unknown_error"))
        message = str(reply.get("message", error))
        if error in _PRESENCE_ERRORS or error == "keychain_error":
            raise KeychainAgentError(f"the agent could not use the keychain ({error}): {message}")
        raise KeychainAgentError(f"the agent refused {cmd} ({error}): {message}")


_fell_back_ever = False
_in_use = False


def fell_back_ever() -> bool:
    """True if any request of this process had to go to the keychain itself, even if the agent
    has answered since (``auth keychain`` records what a whole run authorized)."""
    return _fell_back_ever


def in_use() -> bool:
    """True once this process put the agent in front of its keychain access.

    What the authorization gate looks at, not ``applies()``: a process that already settled on the
    direct keyring keeps it, so an agent binary that shows up later must not waive the gate for
    keychain calls that this process makes itself.
    """
    return _in_use


def mark_in_use(value: bool = True) -> None:
    global _fell_back_ever, _in_use
    _in_use = value
    if not value:
        _fell_back_ever = False


def applies() -> bool:
    """True when keychain access should go through the agent (macOS, binary built, not disabled)."""
    if sys.platform != "darwin" or os.environ.get(DISABLE_ENV, "") == "0":
        return False
    return binary_path().is_file()

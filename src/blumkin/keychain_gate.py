"""Ask for macOS keychain access once, from a terminal, and never from the background (#759).

macOS asks "Python wants to use your confidential information" the first time a *new binary*
touches a keychain item. An ad-hoc signed Python (Homebrew's) is a new binary after every
update, so a process nobody is watching (an MCP server, a cron or agent run) can raise a
dialog that nobody answers, and the command then fails with a timeout.

This module records, per Python build, that a person has gone through that prompt on purpose
(``blumkin auth keychain``). From then on:

- a process with a terminal is let through (the prompt, if any, is expected and answerable);
- a process without one is let through only when this Python build was authorized;
- otherwise it fails at once with the command to run, and never raises a dialog.

The marker lives in the config directory and holds a digest of the Python executable, so a
Homebrew update (a different binary) needs authorizing again, exactly as macOS itself does.
``BLUMKIN_KEYCHAIN_GATE=off`` turns the check off (tests, unusual setups).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from blumkin import keychain_agent, keychain_unlock
from blumkin.config import config_dir

AUTHORIZE_COMMAND = "blumkin auth keychain"
GATE_ENV = "BLUMKIN_KEYCHAIN_GATE"
MARKER_NAME = "keychain_authorized.json"

_identity: str | None = None
_noticed = False


def authorize() -> Path:
    """Record that this Python build went through the keychain prompt on purpose."""
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"authorized_at": datetime.now(UTC).isoformat(), "identity": identity()})
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(body)
    os.replace(tmp, path)
    keychain_unlock.note_unauthorized(None)
    return path


def identity() -> str:
    """A digest of the Python executable: what macOS tells apart, as far as blumkin can see."""
    global _identity
    if _identity is None:
        digest = hashlib.sha256()
        try:
            with open(os.path.realpath(sys.executable), "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            digest.update(sys.executable.encode())
        digest.update(sys.version.encode())
        _identity = digest.hexdigest()
    return _identity


def interactive() -> bool:
    """True when a person is at a terminal here and a macOS dialog could be shown to them.

    The access-list prompt is a GUI dialog: over SSH, or outside the Aqua login session, a
    terminal exists but nobody can answer it there.
    """
    return sys.stdin.isatty() and sys.stderr.isatty() and keychain_unlock.gui_available()


def is_authorized() -> bool:
    """True when the marker names this Python build."""
    try:
        data = json.loads(marker_path().read_text())
    except OSError, ValueError:
        return False
    return isinstance(data, dict) and data.get("identity") == identity()


def marker_path() -> Path:
    return config_dir() / MARKER_NAME


def require(*, direct: bool = False) -> None:
    """Let a keychain call through, or raise ``KeychainNotAuthorizedError`` without touching it.

    With the agent as the keychain client (#773) this process asks the agent and holds no
    keychain access of its own, so there is nothing to authorize here; ``direct`` is for the
    fallback that does touch the keychain itself because the agent cannot be reached.
    """
    global _noticed
    if sys.platform != "darwin" or _switched_off():
        return
    if not direct and keychain_agent.in_use():
        return
    if is_authorized():
        # Authorized meanwhile (another terminal ran the command): forget an earlier refusal.
        keychain_unlock.note_unauthorized(None)
        return
    if interactive():
        if not _noticed:
            _noticed = True
            print(
                "blumkin: macOS may ask for your login keychain password; choose Always Allow "
                f"(run `{AUTHORIZE_COMMAND}` to do this deliberately, once).",
                file=sys.stderr,
            )
        return
    message = (
        "this Python build has not been authorized to use the macOS keychain, and there is no "
        "terminal in a GUI login session to answer macOS's prompt; run "
        f"`{AUTHORIZE_COMMAND}` in a terminal on the Mac itself "
        f"(from an agent session: `! {AUTHORIZE_COMMAND}`), then retry"
    )
    keychain_unlock.note_unauthorized(message)
    raise keychain_unlock.KeychainNotAuthorizedError(message)


def state() -> str:
    """``authorized`` | ``needs_authorization`` | ``via_agent`` | ``not_applicable`` (``doctor``).

    ``via_agent`` is what is configured (the agent is built and not switched off): keychain access
    then goes through the agent, and falls back to the gated direct keyring only when the agent
    cannot be started.
    """
    if sys.platform != "darwin" or _switched_off():
        return "not_applicable"
    if keychain_agent.applies():
        return "via_agent"
    return "authorized" if is_authorized() else "needs_authorization"


def _switched_off() -> bool:
    return os.environ.get(GATE_ENV, "").lower() == "off"

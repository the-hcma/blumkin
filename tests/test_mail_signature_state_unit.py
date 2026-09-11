"""The per-profile cache of Outlook's auto-signature probe result."""

from __future__ import annotations

from pathlib import Path

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.mail_signature_state import (
    SignatureState,
    clear_signature_state,
    load_signature_state,
    record_signature_state,
)
from blumkin.providers.kind import ProviderKind


def _cfg(tmp_path: Path) -> BlumkinConfig:
    cfg = BlumkinConfig(
        client_id="x",
        config_dir=tmp_path,
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=None,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.MICROSOFT,
        tags=(),
        tenant_id="t",
        wo1162425_scopes=False,
    )
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_missing_state_is_unknown(tmp_path: Path) -> None:
    state = load_signature_state(_cfg(tmp_path))
    assert state == SignatureState()
    assert state.detected is None
    assert state.suppresses_signature is False


def test_round_trip_detected(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    written = record_signature_state(cfg, detected=True)
    assert written.detected is True
    assert written.checked_at and written.checked_at.endswith("Z")
    reread = load_signature_state(cfg)
    assert reread.detected is True
    assert reread.suppresses_signature is True
    assert cfg.mail_signature_state_path.is_file()


def test_record_none_does_not_write(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    record_signature_state(cfg, detected=None)
    assert not cfg.mail_signature_state_path.is_file()
    assert load_signature_state(cfg).detected is None


def test_record_false_then_true_overwrites(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    record_signature_state(cfg, detected=False)
    assert load_signature_state(cfg).detected is False
    record_signature_state(cfg, detected=True)
    assert load_signature_state(cfg).detected is True


def test_clear_removes_the_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    record_signature_state(cfg, detected=True)
    clear_signature_state(cfg)
    assert not cfg.mail_signature_state_path.is_file()
    clear_signature_state(cfg)  # idempotent


def test_corrupt_file_is_unknown(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.mail_signature_state_path.write_text("not json{")
    assert load_signature_state(cfg).detected is None


def test_non_bool_detected_is_unknown(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.mail_signature_state_path.write_text('{"outlook_signature_detected": "yes"}')
    assert load_signature_state(cfg).detected is None


def test_state_from_a_different_provider_is_ignored(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)  # provider=microsoft
    cfg.mail_signature_state_path.write_text(
        '{"outlook_signature_detected": true, "provider": "google"}'
    )
    assert load_signature_state(cfg).detected is None
    # A matching provider is honoured.
    cfg.mail_signature_state_path.write_text(
        '{"outlook_signature_detected": true, "provider": "microsoft"}'
    )
    assert load_signature_state(cfg).detected is True

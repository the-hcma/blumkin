"""Interactive ``blumkin loop init`` for mailbox profiles (issue #503).

Authenticates each requested profile once (browser allowed). Successful init
clears a renewal pause so the runner resumes polling that profile.
"""

from __future__ import annotations

from typing import Any

from blumkin.loop.mail_profiles import resume_profile


class LoopInitError(ValueError):
    """Loop mailbox initialization could not complete."""


def init_profiles(
    config_dir: Any,
    *,
    profiles: list[str] | None = None,
    list_profiles: Any | None = None,
    load_config: Any | None = None,
    login_microsoft: Any | None = None,
    login_google: Any | None = None,
) -> dict[str, Any]:
    """Authenticate mailbox profiles for the loop feeder; resume each on success."""
    from blumkin.config import list_profiles as default_list
    from blumkin.config import load_config as default_load

    list_fn = list_profiles or default_list
    load_fn = load_config or default_load
    wanted = {p.strip() for p in (profiles or []) if p.strip()}
    rows = list_fn()
    names: list[str] = []
    for row in rows:
        name = ""
        if isinstance(row, dict):
            name = str(row.get("name") or row.get("profile") or "").strip()
        if not name:
            continue
        if wanted and name not in wanted:
            continue
        names.append(name)
    if wanted and not names:
        missing = ", ".join(sorted(wanted))
        raise LoopInitError(f"Expected configured mailbox profile(s) {missing}, got none")
    if not names:
        raise LoopInitError("Expected at least one mailbox profile in config.toml, got none")

    results: list[dict[str, Any]] = []
    for name in sorted(set(names)):
        cfg = load_fn(profile=name)
        provider = getattr(cfg, "provider", "microsoft")
        try:
            if provider == "google":
                login = login_google
                if login is None:
                    from blumkin.providers import google_auth

                    login = google_auth.login
                status = login(cfg)
            else:
                login = login_microsoft
                if login is None:
                    from blumkin import auth

                    login = auth.create_credential
                # create_credential returns a credential; status_dict is friendlier.
                login(cfg, allow_interactive=True)
                from blumkin import auth

                status = auth.status_dict(cfg)
            resume_profile(config_dir, name)
            results.append({"ok": True, "profile": name, "provider": provider, "status": status})
        except Exception as exc:  # noqa: BLE001 — report per profile
            results.append(
                {
                    "ok": False,
                    "profile": name,
                    "provider": provider,
                    "error": type(exc).__name__,
                }
            )
    return {
        "ok": all(row["ok"] for row in results),
        "profiles": results,
    }

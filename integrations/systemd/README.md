# systemd user unit for `blumkin loop`

The loop runner (issue #501 / #499) is meant to run as a systemd user service so it starts with the session and keeps renewing mail credentials without a terminal. Full operator guide: [`docs/loop.md`](../../docs/loop.md).

## Install

```bash
blumkin loop install-service
systemctl --user daemon-reload
systemctl --user enable --now blumkin-loop
```

Or copy this directory's `blumkin-loop.service` into `~/.config/systemd/user/` and edit `ExecStart` to the absolute path of `blumkin` on that machine.

## Kill switch

Create `~/.config/blumkin/loop/KILL` (or set `kill_switch = true` under `[loop]` in config.toml) to stop every run without stopping the service process. Remove the file (or set the flag false) to allow runs again.

# Retrospective — M1 CLI ship (PR #10)

Closes [#11](https://github.com/the-hcma/blumkin/issues/11).  
Context: [PR #10](https://github.com/the-hcma/blumkin/pull/10) delivered the first slice of epic [#9](https://github.com/the-hcma/blumkin/issues/9).

---

## What went well

- Stack worktree + `gh-stack` submit stayed clean; signed commits without Co-authored-by.
- Hermetic CI (`python-static` + `pytest -m 'not live'`) caught real issues; live Graph covered locally via `BLUMKIN_LIVE=1`.
- Reply-before-resolve + `wait-for-agent-review` kept agent-review threads auditable.
- Review pressure produced durable fixes: minimal Graph scopes, `get_token` verify on login, `0600` secret writes, single atexit handler, `save_token_cache` bound-path guard.

## What hurt

- **Vortex / agents repeatedly flagged `except A, B:` as a SyntaxError.** On Python 3.14 + Ruff `target-version = "py314"`, unparenthesized multi-except is valid and `ruff format` may strip parentheses. Cost several review cycles (documented in `AGENTS.md`).
- **Bugbot disabled** for the repo → skip noise + quota fallback churn (decision below).
- **CodeRabbit quota / rate-limit stubs** blocked `complete_ready` even when threads were clear; Copilot request reliability was uneven on later stacks.
- **Every push re-triggered Vortex**, extending the loop after fixes.
- Primary-clone **README / PLAN status** lagged until the M1 closeout docs PR.

---

## Action checklist

| Action | Status |
|--------|--------|
| Document py314 + Ruff unparenthesized multi-except for agents/reviewers | Done ([#15](https://github.com/the-hcma/blumkin/issues/15) / [#23](https://github.com/the-hcma/blumkin/pull/23)) |
| Refresh README / PLAN Phase 1 checkboxes | Done (#15 / #23) |
| Confirm cold-path install (`uv tool install` → `blumkin` on `PATH`) | Done ([#16](https://github.com/the-hcma/blumkin/issues/16) / #23) |
| Require **Python lint & format checks** + **Pytest (hermetic)** on protection / MQ | Done ([#21](https://github.com/the-hcma/blumkin/issues/21) / #23) |
| Decide Bugbot on/off and align `AGENT_REVIEW_QUOTA_FALLBACK_CHAIN` | **Decided** (below) — validate TODO open |

---

## Bugbot decision

**Keep `bugbot` in the quota fallback chain** (default
`coderabbit,copilot,bugbot` per repository-helpers).

**Assumption:** Bugbot will be (re)enabled for `the-hcma/blumkin` (org/product
rights). Until then, operators may temporarily set
`AGENT_REVIEW_QUOTA_FALLBACK_CHAIN=coderabbit,copilot` to avoid disabled-stub
churn.

- [ ] **TODO (validate):** after Bugbot is enabled on this repo, confirm a real
  Bugbot review lands on a PR head (not a disabled / skip stub). Prefer that
  head also has a usable CodeRabbit or Copilot sign-off so `complete_ready`
  can go true without quota dead-ends.

See `AGENTS.md` (agent review) for the operator note.

---

## Identity / Phase 4 posture (forward)

Phases 2–3 mail/calendar writes and chat reads are shipped. Phase 4 chat write +
meeting get/transcription CLI skills are **implemented with mocked tests**.

**Assumption:** the private-lab Identity / Remedy follow-up will grant the
requested delegated add-ons (details stay in the private lab — not this repo).

- [ ] **TODO (validate live):** after grant — add **new** scopes to the Entra client,
  delete the token cache + auth record under the effective config dir
  (`BLUMKIN_CONFIG_DIR`, else `$XDG_CONFIG_HOME/blumkin` if set, else `~/.config/blumkin/`), run
  `blumkin auth login`, confirm consent includes at least `Chat.ReadWrite` and
  `OnlineMeetings.ReadWrite`, then smoke `chat send|edit|delete` +
  `meeting get|transcription` against Graph.

Tracked in `HANDOFF.md` and `PLAN.md` § Phase 4.

---

## Links

- Epic: [#9](https://github.com/the-hcma/blumkin/issues/9)
- PR: [#10](https://github.com/the-hcma/blumkin/pull/10)
- Issue: [#11](https://github.com/the-hcma/blumkin/issues/11)

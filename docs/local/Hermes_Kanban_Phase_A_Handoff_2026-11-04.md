# Hermes Kanban — Phase A: Reset-aware cooldown

**Handoff date:** 2026-11-04
**Status:** Implemented, tested and installed in the active Acer Hermes checkout; Gateway running.
**Scope:** Hermes Kanban generally (all boards handled by this installation), **not** only Market Radar.

## 1. Purpose

Prevent the same Kanban task from repeatedly spawning a worker at a fixed interval when an LLM provider reports a rate limit with a known quota-reset timestamp. Before Phase A, a `rate_limited` run could be retried after the default ~300-second cooldown even if the provider said the quota would reset much later.

**Phase A is per-task, not a provider/account-wide circuit breaker.** Other tasks and boards may still attempt work and encounter the same exhausted quota.

## 2. Environment and Git identifiers

| Item | Value |
|---|---|
| Active device / OS | Acer, Linux (Ubuntu) |
| Active installation | `/home/bogdan/.hermes/hermes-agent` |
| Installed baseline | `bf0203a794a07aac3ff764972ab9087baa4e4f4e` — tag `v0.22.5` |
| Active installation branch | `local/kanban-reset-aware` |
| Phase A commit | `47f6f4b80b7` (abbreviated; check full SHA with `git rev-parse HEAD`) |
| Commit title | `fix(kanban): respect provider reset deadlines on rate limits` |
| Pre-install rollback tag | `backup/pre-kanban-phase-a-v0.22.5` |
| Independent development clone | `/home/bogdan/Projects/hermes-rate-limit-standalone` |
| Development branch | `fix/kanban-reset-aware-cooldown` |
| GitHub fork | `https://github.com/Tochi2502/hermes-agent` |
| Patch branch link | `https://github.com/Tochi2502/hermes-agent/tree/fix/kanban-reset-aware-cooldown` |
| Gateway service | `hermes-gateway.service` (`systemctl --user`) |

The earlier directory `/home/bogdan/Projects/hermes-rate-limit-fix` is a **linked worktree** whose Git metadata lives under the active installation `.git/worktrees/`; it triggered test home-I/O safety protections. Do further development and tests in the **independent standalone clone**.

An additional pre-standalone working diff backup exists at `/home/bogdan/hermes-rate-limit-phase-a.patch`; it predates later validation/docstring revisions, so **prefer the committed branch**, not the old patch, as the source of truth.

## 3. Implemented flow

`Agent result.failure_resets_at` → `gateway/session_kanban.py` → Kanban `worker_result` event → `hermes_cli/kanban_owner_recovery.py` → `hermes_cli/kanban_db_dispatch.py` reclaim → `task_runs.metadata.retry` → `check_respawn_guard()`.

The Gateway saves a structured versioned retry record only on exit code `75` with a valid future provider reset time:

```json
{
  "retry": {
    "version": 1,
    "reason": "rate_limit",
    "not_before": 1900000000.0,
    "deadline_source": "provider_reset"
  }
}
```

The example timestamp above illustrates the schema; **it is not a real observed reset time**.

Validation includes numeric non-boolean finite timestamps and a maximum horizon of seven days. The guard also checks `version == 1`, `reason == "rate_limit"`, `deadline_source == "provider_reset"`, and bounds relative to the run end time; it holds a task until the provider deadline plus a ten-second margin. Older runs without valid retry metadata use the standard configured cooldown (`HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS`; default 300 seconds). A configured zero standard cooldown does not override a valid provider deadline.

All logic is shared by the dispatcher; individual Kanban boards have separate state databases, but do **not** require separate patch installation.

## 4. Files changed in the committed patch

Production:

- `gateway/session_kanban.py` — transports valid `failure_resets_at` into a Kanban `worker_result` event.
- `hermes_cli/kanban_owner_recovery.py` — adds `managed_run_result()` with current-run PID/claim-lock verification; retains `managed_run_exit_code()` compatibility accessor.
- `hermes_cli/kanban_db_dispatch.py` — preserves retry metadata on run reclaim and honors validated provider deadline in `check_respawn_guard()`.

Tests:

- `tests/gateway/test_kanban_result_exit.py` — event-writing cases including malformed reset values.
- `tests/hermes_cli/test_kanban_managed_result_pid.py` — event → run metadata and guard behavior.
- `tests/hermes_cli/test_kanban_db.py` — standard-cooldown compatibility and 13 provider-deadline edge cases.

## 5. Validation and production rollout

The final combined automated run in the independent clone covered five test files:

```text
137 passed, 0 failed, 1 skipped (Windows-only test on Linux)
```

Test command (does not intentionally call an LLM provider):

```bash
cd ~/Projects/hermes-rate-limit-standalone
scripts/run_tests.sh \
  tests/gateway/test_kanban_result_exit.py \
  tests/hermes_cli/test_kanban_managed_result_pid.py \
  tests/hermes_cli/test_kanban_db.py \
  tests/hermes_cli/test_kanban_host_cap.py \
  tests/hermes_cli/test_kanban_review_lifecycle.py \
  -q --tb=short 2>&1 | tee ~/hermes-phase-a-final-tests.txt
```

Rollout performed:

1. Confirmed baseline was `bf0203a` / `v0.22.5`.
2. Created active checkout branch `local/kanban-reset-aware`.
3. Created `backup/pre-kanban-phase-a-v0.22.5` Git tag.
4. Fetched patch commit from the GitHub fork.
5. Verified fast-forward ancestry and a clean working tree.
6. Stopped `hermes-gateway.service`.
7. Applied `git merge --ff-only 47f6f4b80b` in active checkout.
8. Restarted Gateway; journal showed normal startup and no patch-related traceback in the inspected excerpt.
9. Verified `systemctl --user is-active hermes-gateway.service` → `active`; active HEAD displayed `47f6f4b80b7`.

**Production limitation:** real provider HTTP 429 → quota-reset scheduling has **not** been deliberately exercised end-to-end in the live Gateway. Avoid inducing LLM rate limits merely to test it.

## 6. Safe rollback — operator procedure (not yet executed)

**Do not execute rollback unless necessary.** This changes the running installation and temporarily stops Gateway; use a maintenance window and verify that no active worker/task will be interrupted. Preserve the patched branch/commit for later recovery.

First inspect the current active checkout; do not assume it remains at Phase A after a future update:

```bash
git -C ~/.hermes/hermes-agent status --short --branch
git -C ~/.hermes/hermes-agent log -1 --oneline
git -C ~/.hermes/hermes-agent rev-parse backup/pre-kanban-phase-a-v0.22.5
```

**If and only if** the checkout is clean and still on the intended Phase A commit, a controlled rollback can move the local branch to the saved pre-patch tag. Stop Gateway first, then use Git's `--ff-only`-incompatible backward movement **only after explicit operator confirmation**. A safer approach is to create a new rollback branch at the tag and switch onto it while the service is stopped, then restart and verify. Example (do not execute blindly on a later updated checkout):

```bash
systemctl --user stop hermes-gateway.service
git -C ~/.hermes/hermes-agent switch -c local/pre-phase-a-rollback backup/pre-kanban-phase-a-v0.22.5
systemctl --user start hermes-gateway.service
systemctl --user is-active hermes-gateway.service
git -C ~/.hermes/hermes-agent log -1 --oneline
```

This preserves the `local/kanban-reset-aware` branch and avoids `git reset --hard`. If the rollback branch already exists or the checkout is dirty, **stop and review** rather than forcing it. Confirm the service and dashboard afterward. The rollback tag contains source state, **not** a backup of runtime databases or secrets.

## 7. Future `hermes update` policy

**Do not run `hermes update` on the patched production checkout without reviewing the update target and preserving the patch.** The updater contains Git stash/reset paths as well as custom-branch merge handling. Custom-branch code attempts merges rather than simply discarding commits in some situations, but merge conflicts are possible and retention is not guaranteed in every path.

Recommended workflow:

1. Review upstream changes and the exact target commit/version in the standalone clone.
2. Merge or cherry-pick the patch in a **testing branch** against the newer upstream version; resolve conflicts there.
3. Re-run relevant Gateway/Kanban tests in the independent clone.
4. Record a new tested commit, push to fork, and create a new production rollback tag.
5. Schedule the production update/merge with a controlled Gateway restart and read-back checks.

Avoid using the older `.patch` backup on top of an updated upstream version without a compatibility review.

## 8. Out of scope and known constraints

- No-reset deadline scenario: if `failure_resets_at` is absent or invalid, the old ~300-second fixed retry remains. Proposed **Phase B:** bounded adaptive backoff for this case.
- Cross-task/board resource coordination: this patch does **not** quarantine all tasks using the same depleted account. Proposed **Phase C:** shared provider/account quota ledger or circuit breaker.
- Pre-existing `blocker_auth` holds on already queued tasks are not retroactively cleared by Phase A; diagnosing these is a **separate task**.
- Telegram connectivity warnings and optional toolset warnings are unrelated to Phase A; do not bundle those investigations with this patch.
- No active Kanban task statuses were deliberately modified during installation.

## 9. Next decision

**Phase A can be closed as code-complete, tests-pass, and installed with Gateway verified up.** Keep production validation of a naturally occurring 429 as a follow-up observation, not a prerequisite requiring forced traffic. Prioritize Phase B only as a separate scoped task if repeated no-reset 429 attempts recur.

## Operator preferences

- **One Step → Verify** for dependent terminal actions.
- Commands with potentially long output (~10+ lines): use `tee` to preserve logs; short checks do not need `tee`.
- Before troubleshooting, verify the relevant device, service instance and database path; do not guess.
- Do not spend Hermes/Codex quota on checks that can be handled by shell, Git and local tests.
- Keep tasks in scope and avoid chasing unrelated warnings.

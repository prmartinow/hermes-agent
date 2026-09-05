# Implementation Plan: `/gswitch` Gemini Account Manual Pinning Feature

**Document Path**: `docs/plans/gswitch_implementation_plan.md`  
**Command Syntax**:  
- `/gswitch <label>` — Pins the active chat to that account (e.g. `alias1`, `alias2`, `alias3`)
- `/gswitch auto` or `/gswitch clear` — Unpins the chat and restores dynamic CD-DOCI rotation

---

## 1. Subgoals & Checklist

1. **`gswitch-registry`**: Register `/gswitch` and `/gacc` slash commands in `hermes_cli/commands.py`.
2. **`gswitch-core-handler`**: Implement `handle_gswitch_command()` in `hermes_cli/auth.py` with dynamic label discovery, database model_config updates, and live `_swap_credential()` binding.
3. **`gswitch-cli`**: Add `/gswitch` command handler in `HermesCLI.process_command()` in `cli.py`.
4. **`gswitch-tui-gateway`**: Add `/gswitch` to `_LIVE_SESSION_DIRECT_COMMANDS` in `tui_gateway/server.py` and `command.dispatch` in `tui_gateway/methods_tools.py`.
5. **`gswitch-desktop-spec`**: Add `gswitch` to `DESKTOP_COMMAND_SPECS` in `apps/desktop/src/lib/desktop-slash-commands.ts`.
6. **`gswitch-test-suite`**: Authored test suite in `tests/test_gswitch_command.py` verifying all success and error behaviors via `scripts/run_tests.sh`.

---

## 2. Invariants & Output Specifications

- `/gswitch alias2` $\to$ `✓ Switched to alias2`
- `/gswitch auto` $\to$ `✓ Restored dynamic CD-DOCI rotation`
- `/gswitch clear` $\to$ `✓ Restored dynamic CD-DOCI rotation`
- `/gswitch 2` $\to$ `✗ Invalid account. Use configured account labels (or auto)`
- `/gswitch foo` $\to$ `✗ Unknown account 'foo'. Available: alias1, alias2, ..., auto`
- `/gswitch` $\to$ `Usage: /gswitch <label|auto|clear> (e.g. /gswitch alias2, /gswitch auto)`

# Project History & Pitfalls (`PROJECT_HISTORY.md`)

> RULE|chronological archive ONLY (`SessionStart` injects live git/versions/queue) -> `!static("Current State"|"Current Version")`.
> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim `path:line`, `symbol()`, invariants).

---

## 1. Env & Stack
<!-- ENV|runtime/build/test cmds & key constraints -->

## 2. Pitfalls & Invariants
<!-- FORMAT: - PITFALL|<path:sym> -> <root-cause/constraint> -> <safe fix> -->

## 3. Version History (Newest First)
<!-- RULE: PREPEND 1-3L entries directly below (newest first). !static("Current State"). -->
<!-- FORMAT: - YYYY-MM-DD|<tag>|<scope/file:sym> <change> -> <effect> | verified:<cmd> -->

- `Plugin v2.17.0 [x]` (2026-11-24): anti-polling PreToolUse guards, `--fast` auto-rewrite, & single-verify-owner discipline: (1) new `hooks/planner_guard_polling.py` wired in `hooks.json` (`PreToolUse` `schedule|manage_task`) blocks timer polling via `schedule` (`DurationSeconds` + polling prompt or `<=30s` unconditional `!IsDaemon`) -> directs reactive wakeup (`NotificationTimeoutSeconds` + empty reply) or `gh run watch --exit-status`, & denies repeat `manage_task(status|list)` within 90s via `hook_state["task_status_polls"]` (allowing `kill`/`send_input`/`IsDaemon`/`CronExpression`); (2) `hooks/planner_guard_command.py` adds `check_polling_command()` blocking repeat `ps aux | grep` (`dotnet|msbuild|python|pytest`) within 120s (`hook_state["ps_polls"]`) + shell `while/for+sleep` loops polling `gh run list|view`, plus `_inject_plugin_tests_fast()` auto-appending `--fast` to bare `run_plugin_tests.py` commands; (3) `hooks/planner_pre_invocation.py` preserves poll windows across turns & tightens `DELEGATE|` (`<=200` chars) with `!poll(!schedule/!manage_task-loop)` + `1-verify-owner`; (4) updated `rules/AGENTS.md`, `skills/dynamic-planner/SKILL.md`, `.agents/docs/*`, & added `TestAntiPollingGuard` in `tests/run_plugin_tests.py` | verified:`tests/run_plugin_tests.py --fast`

- `Plugin v2.16.0 [x]` (2026-11-24): configurable nested sub-subagent & Google medium/low tiering (`delegation.sub_subagent`): (1) `scripts/schema_migrations.py` (`DEFAULT_DELEGATION_CONFIG.sub_subagent`: `allow_nesting:true`, `default_model:"flash_lite"`, `swarm_medium_model:"gemini-4-hydrogen-medium"`, `allow_medium_tier:true`) + `resolve_delegation_config()` deep nested dict merge (preserving `profile_v1..v4` compat); (2) `hooks/planner_guard_delegation.py` allows nested `invoke_subagent` `flash_lite`/`flash` & `swarm` `gemini-*-medium`/`gemini-*-low` for non-complex slices when `allow_medium_tier:true` (enforces `allow_nesting:false` & `allow_medium_tier:false` + strict `!Claude/!Anthropic/!OpenAI/!ChatGPT/!o1-o4` block); (3) `hooks/planner_pre_invocation.py` injects compact `sub-sub:` cue in `DELEGATE|` (`<=200` chars); (4) updated `rules/AGENTS.md`, `skills/dynamic-planner/SKILL.md`, `Cavern` `.agents/planner_profile.json`, & `tests/run_plugin_tests.py` | verified:`tests/run_plugin_tests.py`

- `Plugin v2.15.0 [x]` (2026-11-24): adaptive `pace=turbo|eco|normal` parallel cubicle engine, zero-quota bidirectional `global<->embedded` `SessionStart` auto-sync, & automatic host `PROJECT_HISTORY.md` sync: (1) added adaptive delegation pace (`turbo` default parallel isolated subagent cubicles; `eco` preserves parallel cubicles whenever net-token-saving; `normal` standard adaptive routing); (2) upgraded `SessionStart` plugin sync to zero-quota bidirectional `global<->embedded` higher-version auto-sync (never downgrading); (3) wired automatic host `PROJECT_HISTORY.md` updates/sync alongside version & handoff ledgers; (4) synced `plugin.json`, `rules/AGENTS.md`, `skills/{dynamic-planner,sota-blueprint}/SKILL.md`, `CHANGELOG.md`, & `PROJECT_HISTORY.md`/`EXTERNAL_AI_SYNC.md` | verified:docs+manifests

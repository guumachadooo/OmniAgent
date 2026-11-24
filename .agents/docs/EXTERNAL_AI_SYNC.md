# External AI Sync Ledger (`EXTERNAL_AI_SYNC.md`)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim `path:sym`).
> FORMAT|`- <YYYY-MM-DD> | <pending-push|commit> | <scope> | <files> | <change_or_root_cause> | checks:<manual|cmds> | ci_apk:<needed|skip> | next:<pending>`

## Project Sync Entries (Newest First)
- 2026-11-24 | pending-push | omniagent-cavern-v2.14.0-parity | AGENTS.md, .agents/{federation,host_matrix,planner_profile}.json, .agents/docs/*, .agents/plugins/dynamic-fractal-planner/*, agent/{bridge_auth,local_capabilities,omni_agent_client,repo_federation,termux_auto_pilot,test_omni_agent}.py | unified OmniAgent onto Cavern multi-host + federation + dynamic-fractal-planner v2.14.0 standard (embedded plugin, schema v5 profile, 10-entry host_matrix.json, W48.b+v2.14.0 agent mirror, zero-quota sync-plugin & --sync-plugins auto-pilot) | checks:test_omni_agent(23/23)+host-audit+verify_universal | ci_apk:skip | next:keep synced via sync-plugin

## Plugin (`dynamic-fractal-planner`) Sync Entries (Newest First)
- 2026-11-24 | pending-push | plugin-v2.14.0-universal-fleet-sync-and-fastpath | .agents/plugins/dynamic-fractal-planner/* | embedded dynamic-fractal-planner v2.14.0 (universal fleet sync, .workspaces.json registry, equal-semver mtime tie-break, [PLUGIN_SYNC_FASTPATH] 1-cmd 0-thinking hook, bootstrap --embed-plugin) | checks:run_plugin_tests(68/68) | ci_apk:skip | next:synced across Cavern, OmniAgent, DFP & global

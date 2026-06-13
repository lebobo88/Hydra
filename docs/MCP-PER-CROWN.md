# MCP-per-Crown — Public-Facing Server Inventory

The manifesto Part III §3 says: *"ship MCP servers for every crown so
third-party Claude Desktop / Cursor / Kiro users can plug into individual
heads."* This is the interoperability bet — Hydra speaks MCP outward as
well as inward.

## Current state (Stage 5+)

Hydra ships the following in-repo MCP servers under `mcp_servers/`, wired
via `.mcp.json` (gateway mode consolidates them behind `hydra_gateway`):

| Server | Module | Status | Crown affinity |
|---|---|---|---|
| `pp-daemon` | external (pair-programmer daemon) | shipped | Forge |
| `hydra-memory` | `mcp_servers.hydra_memory` | shipped | substrate (TheEights) |
| `executive-suite` | `mcp_servers.executive_suite` | shipped | Executive |
| `rlm-creative` | `mcp_servers.rlm_creative` | shipped | Garland |
| `senate` | `mcp_servers.senate` | shipped | Curia (legal-compliance) |
| `marketbliss` | `mcp_servers.marketbliss` | shipped | Marketing |
| `xenia` / `xenia-kb` | `mcp_servers.xenia`, `mcp_servers.xenia_kb` | shipped | Hearth (customer-support) — roster + knowledge-base read |
| `xenia-tickets` | `mcp_servers.xenia_tickets` | shipped | Hearth — customer-support ticket filing (side-effecting) |
| `hydra-control` | `mcp_servers.hydra_control` | shipped | supervisor — workflow-resume gate + cockpit audit filing |
| `hydra-toolshed` | `mcp_servers.hydra_toolshed` | shipped | meta — search/describe/execute over large catalogs |
| `hydra-gateway` | `mcp_servers.hydra_gateway` | shipped | meta — unified backend proxy |

Tools exposed today (verified against `server.py#_tool_handlers`):

- **hydra-memory**: `hydra-mem.write_episodic`,
  `hydra-mem.read_episodic`, `hydra-mem.list_workflow`,
  `hydra-mem.semantic_search`, `hydra-mem.query_eights`,
  `hydra-mem.tag_memory`, `hydra-mem.ping` (+ mesh-console read surface:
  `workflows_list`, `workflow_status`, `squad_list`, `hitl_pending`).
- **executive-suite**: roster lookup, skill catalog, command catalog,
  output write — agent-impersonation pattern.
- **rlm-creative**: skill catalog, command catalog, output write.
- **hydra-control** (3 tools): `hydra.control.ping` (no-arg liveness),
  `hydra.workflow.resume` (launches the CLI resume path for a paused HITL
  gate — `approve | reject | modify-budget | force-dispatch`),
  `hydra.cockpit.audit` (files a `cockpit_write` audit envelope through the
  attestor for an action taken from the mesh console). This is the
  **workflow-resume gate + cockpit audit filing** server.
- **xenia-tickets** (9 tools): `xenia-tickets.create`, `.get`, `.list`,
  `.comment`, `.update_fields`, `.recommend`, `.send_response`,
  `.execute_approved`, `.ping`. This is the **customer-support ticket
  filing** server for the Xenia Hearth squad. The two mutating tools
  (`send_response`, `execute_approved`) **verify a server-side WS-AUTH
  capability token** (`mint_for_tool.py` + `clearance.py`,
  `verify_operator_capability` semantics) before acting; the executor's
  `actor_id` is taken from the dispatcher binding, never self-reported.
- **pp-daemon**: 42+ tools exposed by the pair-programmer harness.

## What Stage 6 (LIT-4) adds

The crown-level publishing surface so an outside user can install one
crown without standing up the whole supervisor:

### 1. Per-crown MCP manifest

Each crown publishes a stable, named MCP server with a documented tool
surface and a *cathedral-aliased* tool naming convention. Outside users
should see mythic names where the architecture intends:

| Crown | MCP server name | Tool prefix | Example invocation |
|---|---|---|---|
| Executive | `hydra-executive` | `solon.`, `athena.`, `hermes.`, … | `solon.brief`, `athena.compete`, `iris.devils_advocate` |
| Forge | `hydra-forge` | `daedalus.`, `prometheus.`, `cerberus.`, … | `daedalus.design`, `cerberus.audit`, `argus.review` |
| Garland | `hydra-garland` (planned) | `calliope.`, `erato.`, `helios.`, … | `calliope.position`, `helios.shotlist` |
| Substrate | `hydra-eights` (rename of current `hydra-memory` for the public surface; the internal name stays) | `eights.` | `eights.query("kan", workflow_id=…)`, `eights.tag(key, cells)` |

The internal MCP servers do not change. The published servers are thin
re-exports that map mythic names to plaza implementations and apply the
constitution + Cerberus gates per call.

### 2. Cross-cutting requirements

Every published crown MCP MUST:

- pass every tool call through `hydra_core.venom.require_cerberus_pass`
  when the tool is on a venom list,
- redact arguments via `governance.redact_for_squad_boundary` at the
  server boundary,
- tag every memory write with the appropriate cell (the classifier in
  `eights.classifier` does this on the inside; the published server
  just passes through),
- carry a stable schema version so Claude Desktop / Cursor / Kiro can
  pin a tool surface across upgrades.

### 3. Installation story

Each published crown ships:

- A `mcp.json` snippet third parties can paste into their host config.
- A README in plaza voice describing the tool surface and the head's
  refusal patterns (the user sees what the head will and will not do).
- A signed manifest (SHA-256 of the server module) so the host can
  detect drift.

### 4. Naming and discovery

The public namespace is `hydra-*`. The internal namespace stays as it is
(`hydra-memory`, `executive-suite`, `rlm-creative`, `pp-daemon`). The
mapping table above is the only seam.

## Open work for LIT-4 (not in this run)

- Implement the three thin re-export servers (`hydra-executive`,
  `hydra-forge`, `hydra-garland`).
- Publish to the major MCP catalogues / directories that exist at launch.
- Write the per-crown installation README.
- Add an AAIF MCP Dev Summit presence if the timing lines up
  (the manifesto cites ~1,200 attendees in April 2026).
- Coordinate with pair-programmer maintainers on Forge crown exposure —
  `pp-daemon` already exposes 42 tools; the crown surface is a curated
  subset rather than a re-publication of the full set.

## Decision deferred

Whether to require an API key for the public MCP surface or to ship
unauthenticated and rely on Cerberus + the constitution for safety. The
manifesto leans unauthenticated for the open-source surface; an
authenticated tier is reserved for hosted SaaS in `docs/PRICING.md`
Layer 2.

/**
 * web/server/whitelist.ts
 *
 * Hard read-only tool whitelist for the Hydra Cockpit bridge.
 *
 * INVARIANT (inviolable):
 *   The bridge will refuse to call any tool not in this frozen set.
 *   There are NO write tools in it, by construction.
 *   The forbidden-verb denylist is checked ADDITIONALLY as defense-in-depth —
 *   even if a tool were added to READ_HYDRA_TOOLS by mistake, a forbidden
 *   verb substring blocks it.
 *
 * Read tools (verified against mcp_servers/hydra_memory/server.py lines 340-352):
 *   hydra-mem.ping, hydra-mem.workflows_list, hydra-mem.workflow_status,
 *   hydra-mem.squad_list, hydra-mem.hitl_pending, hydra-mem.read_episodic,
 *   hydra-mem.list_workflow, hydra-mem.semantic_search, hydra-mem.query_eights
 *
 * Excluded write tools (NOT in this list):
 *   hydra-mem.write_episodic  — writes episodic memory
 *   hydra-mem.tag_memory      — mutates tags on episodic rows (write #8 in C3)
 *
 * Forbidden-verb denylist: any tool matching these substrings is ALWAYS refused
 * (defense-in-depth). Note that hydra-mem read tool names use underscores for
 * verbs (e.g. "workflows_list"), NOT dots, so the denylist uses underscore-form
 * patterns for hydra-mem. Dot-form patterns cover future mesh.* tool leakage.
 *
 * No-false-positive check: none of the nine read tools above contain any
 * forbidden substring:
 *   "ping"         → no match
 *   "workflows_list" → no match (list is not forbidden; _list not in denylist)
 *   "workflow_status" → no match
 *   "squad_list"   → no match
 *   "hitl_pending" → no match
 *   "read_episodic"  → "read" is NOT forbidden (it is the read path)
 *   "list_workflow"  → no match
 *   "semantic_search" → no match
 *   "query_eights"  → no match
 * All nine confirmed clean.
 */

/**
 * Frozen read-only Hydra Cockpit tool whitelist.
 * Verified against mcp_servers/hydra_memory/server.py _tool_handlers() dict (2026-06-07).
 */
export const READ_HYDRA_TOOLS = Object.freeze([
  'hydra-mem.ping',
  'hydra-mem.workflows_list',
  'hydra-mem.workflow_status',
  'hydra-mem.squad_list',
  'hydra-mem.hitl_pending',
  'hydra-mem.read_episodic',
  'hydra-mem.list_workflow',
  'hydra-mem.semantic_search',
  'hydra-mem.query_eights',
] as const);

export type ReadHydraTool = (typeof READ_HYDRA_TOOLS)[number];

const READ_SET: ReadonlySet<string> = new Set(READ_HYDRA_TOOLS);

/** Returns true iff `tool` is in the Hydra Cockpit read-only whitelist. */
export function isWhitelisted(tool: string): tool is ReadHydraTool {
  return READ_SET.has(tool);
}

/**
 * Defense-in-depth forbidden-verb denylist.
 * Even if a tool slipped into the whitelist by mistake, matching any of these
 * substrings (case-insensitive) unconditionally blocks the call.
 *
 * Covers both underscore-form (hydra-mem.*) and dot-form (mesh.*) patterns.
 */
export const FORBIDDEN_SUBSTRINGS = Object.freeze([
  // Underscore-form: hydra-mem write/mutate verbs
  'write_',
  'tag_',
  // Dot-form: mesh.* style write verbs (defense against future tool leakage)
  '.write',
  '.tag',
  '.enroll',
  '.unenroll',
  '.restart',
  '.ack',
  '.resolve',
  '.verify',
  '.set',
  '.attest',
  '.approve',
  '.reject',
  '.rollback',
  '.commit',
  '.add',
  '.delete',
  '.update',
  '.mutate',
  '.create',
  '.propose',
  '.register',
  '.unregister',
  '.reset',
  '.stop',
  '.start',
  '.cap',
  '.classify',
  '.link',
  '.sync',
  '.amendment',
  '.outcome',
  '.launch',
  '.replay',
  '.resume',
  '.dispatch',
]);

/** Returns true iff the tool name contains a forbidden verb substring (case-insensitive). */
export function isForbidden(tool: string): boolean {
  const lower = tool.toLowerCase();
  return FORBIDDEN_SUBSTRINGS.some((s) => lower.includes(s));
}

/**
 * The only gate the read path uses.
 * A tool is allowed iff it is whitelisted AND does not contain a forbidden verb.
 */
export function allowTool(tool: string): boolean {
  return isWhitelisted(tool) && !isForbidden(tool);
}

// OpenCode plugin: tells the `cinna` stdio bridge which OpenCode session made
// a dashboard tile call.
//
// OpenCode sends no session information with an MCP tools/call. The bridge
// would otherwise read the backend session id from the one
// session_context.json per mode, which the latest message overwrote, so with
// two conversations in one environment a tile call could act on the other
// chat's dashboard. `tool.execute.before` receives the calling sessionID and
// the same `args` object OpenCode then sends to the MCP server, so the session
// id rides along under a reserved key. The bridge strips it before validating
// the arguments and resolves session_context/<sessionID>.json from it.
//
// Loaded by the OpenCode adapter through the runtime opencode.json `plugin`
// list. No imports, so nothing needs installing.

const DASHBOARD_TOOL_PREFIX = "cinna_dashboards_"
const SESSION_ARG = "_cinna_opencode_session_id"

export const CinnaSessionPlugin = async () => ({
  "tool.execute.before": async (input, output) => {
    if (!input || typeof input.tool !== "string" || !input.tool.startsWith(DASHBOARD_TOOL_PREFIX)) return
    if (!output || typeof output.args !== "object" || output.args === null) return
    if (typeof input.sessionID === "string" && input.sessionID) {
      output.args[SESSION_ARG] = input.sessionID
    }
  },
})

/**
 * Test a deployed managed agent in the Claude Console — the visual session
 * runner at platform.claude.com — or run a single headless task.
 *
 *   bun run console <name>                 # create a session + open it in Console
 *   bun run console <name> -- --once "…"   # headless single task (smoke tests,
 *                                          #   custom-tool round-trips), print reply, exit
 *
 * The web Console can't answer custom tools (this process executes those), so
 * agents with custom tools park at requires_action there — use --once instead.
 * Outcome-mode agents (manifest.invocation === "outcome") treat the --once input
 * as an outcome description and run it against the agent's rubric.md.
 */
import { spawn } from "node:child_process";
import {
  getOrCreateEnvironment,
  loadManagedAgent,
  makeClient,
  runTask,
  type SessionEvent,
} from "@/lib/claude-managed-agent.ts";

const args = process.argv.slice(2);
const [name] = args;
if (!name) {
  console.error(
    'usage: bun run console <name> [-- --once "task"] [--quiet] [--timeout <seconds>] [--mcp-silence <seconds>]'
  );
  process.exit(1);
}
const onceIndex = args.indexOf("--once");
if (onceIndex !== -1 && !args[onceIndex + 1]?.trim()) {
  // Catches `--once "$(cat wrong/path)"` expanding to "" — without this the
  // script would fall through to opening a Console session and exit 0, which
  // reads as a passing smoke test.
  console.error("--once needs a non-empty task");
  process.exit(1);
}
const once = onceIndex === -1 ? undefined : args[onceIndex + 1];
const quiet = args.includes("--quiet");
// Stand the MCP watchdog down. Needed to TEST the missing-tool path: with no
// MCP server reachable the agent makes no MCP call, so the watchdog would kill
// the run before the agent can report the outage it is supposed to report.
const silenceIndex = args.indexOf("--mcp-silence");
const mcpSilenceMs =
  silenceIndex === -1 ? undefined : Number(args[silenceIndex + 1]) * 1000;
if (silenceIndex !== -1 && !Number.isFinite(mcpSilenceMs)) {
  console.error("--mcp-silence needs a number of seconds (0 disables)");
  process.exit(1);
}
const timeoutIndex = args.indexOf("--timeout");
const timeoutMs =
  timeoutIndex === -1 ? undefined : Number(args[timeoutIndex + 1]) * 1000;
if (
  timeoutIndex !== -1 &&
  (!Number.isFinite(timeoutMs) || (timeoutMs ?? 0) <= 0)
) {
  console.error("--timeout needs a positive number of seconds");
  process.exit(1);
}

const { manifest, rubric, tools } = await loadManagedAgent(name);

const agentLabel = manifest.deployment?.agent_id ?? "(not deployed)";
console.error(
  `[${manifest.name}] agent ${agentLabel} · ` +
    `${manifest.invocation} mode · ${tools.length} custom tool(s)`
);

if (!once) {
  if (!manifest.deployment?.agent_id) {
    console.error(`not deployed yet — run: bun run deploy ${name}`);
    process.exit(1);
  }
  if (tools.length > 0) {
    console.error(
      `note: ${tools.length} custom tool(s) run in-process — Console sessions will park on them; ` +
        `use \`bun run console ${name} -- --once "…"\` for those paths.`
    );
  }
  const client = makeClient();
  const environmentId = await getOrCreateEnvironment(client);
  const session = await client.beta.sessions.create({
    agent: manifest.deployment.agent_id,
    environment_id: environmentId,
    title: `${manifest.name}: Console session`,
    ...(manifest.vault_ids?.length ? { vault_ids: manifest.vault_ids } : {}),
  });
  const sessionURL = `https://platform.claude.com/workspaces/default/sessions/${session.id}`;
  console.error(`opening Console session ${session.id}`);
  console.log(sessionURL);
  spawn("open", [sessionURL], { detached: true, stdio: "ignore" }).unref();
  process.exit(0);
}

function renderEvent(event: SessionEvent) {
  if (quiet) {
    return;
  }
  switch (event.type) {
    case "session.status_running":
      console.error("  · running…");
      break;
    case "agent.tool_use":
      console.error(`  · tool: ${String(event.name ?? "?")}`);
      break;
    case "agent.mcp_tool_use":
      // Render MCP calls too. Without this an MCP-driven agent shows only its
      // bash/read calls and looks like it never reached its tools at all.
      console.error(
        `  · mcp: ${String(event.name ?? "?")} ${JSON.stringify(event.input ?? {}).slice(0, 160)}`
      );
      break;
    case "agent.custom_tool_use":
      console.error(
        `  · custom tool: ${String(event.name ?? "?")} ${JSON.stringify(event.input ?? {})}`
      );
      break;
    case "span.outcome_evaluation_start":
      console.error(`  · grader: iteration ${String(event.iteration)}`);
      break;
    case "span.outcome_evaluation_end":
      console.error(`  · grader: ${String(event.result)}`);
      break;
    default:
      break;
  }
}

const result = await runTask({
  manifest,
  mcpSilenceMs,
  onEvent: renderEvent,
  rubric,
  task: once,
  timeoutMs,
  tools,
});
if (result.outcome) {
  console.error(`  · outcome: ${result.outcome.result}`);
}
console.error(
  `  · trace: https://platform.claude.com/workspaces/default/sessions/${result.sessionId}`
);
console.log(result.text);

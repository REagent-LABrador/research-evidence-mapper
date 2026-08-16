/**
 * Runtime for calling deployed Claude Managed Agents.
 *
 * One loop, used by both `scripts/console.ts` (CLI) and the eve tool
 * wrappers in `agent/tools/<name>.ts`:
 *
 *   get-or-create session → send user event → consume SSE →
 *   answer custom-tool calls in-process → return the final agent message.
 *
 * Custom tools are the interesting part: when the managed agent calls one,
 * the session parks at `status_idle` with `stop_reason: requires_action`
 * until *this process* runs the matching handler from the agent's
 * `tools.ts` and posts a `user.custom_tool_result`. The process running
 * this file is the tool executor — no extra infrastructure.
 */
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import Anthropic from "@anthropic-ai/sdk";
import dotenvx from "@dotenvx/dotenvx";
import { z } from "zod";

// ---------------------------------------------------------------------------
// Schemas + types shared with managed agent dirs
// ---------------------------------------------------------------------------

/** One custom tool: declaration (sent to the agent) + local handler. */
export type CustomToolSpec = {
  description: string;
  handler: (input: Record<string, unknown>) => Promise<string> | string;
  input_schema: Record<string, unknown>;
  name: string;
};

/**
 * Shape of `managed/<name>/manifest.json`, validated on load.
 * Loose objects throughout: `scripts/deploy.ts` round-trips the manifest back
 * to disk, so unknown keys must survive a parse.
 */
export const AgentManifest = z.looseObject({
  // Written by `/managed-agent-deploy` at compile time; basis for Claude-merge.
  compiled_hashes: z.record(z.string(), z.string()).optional(),
  // Written by `scripts/deploy.ts`. Absent until first deploy.
  deployment: z
    .looseObject({
      agent_id: z.string(),
      agent_version: z.number(),
      // Provisioned by deploy when the manifest has a `memory` block.
      memory_store_id: z.string().optional(),
      // skills dir name → uploaded skill.
      skills: z.record(
        z.string(),
        z.looseObject({
          hash: z.string(),
          skill_id: z.string(),
          version: z.string(),
        })
      ),
      system_hash: z.string(),
      tools_hash: z.string(),
    })
    .optional(),
  description: z.string().optional(),
  // "message" = conversational turns; "outcome" = rubric-graded deliverable.
  invocation: z.enum(["message", "outcome"]),
  // Outcome mode only. Default 3, max 20.
  max_iterations: z.number().optional(),
  // Remote streamable-HTTP MCP servers, passed through to the agent config.
  // "permission" is compile-time metadata; deploy maps it onto the matching
  // toolset's permission_policy (default "always_ask").
  mcp_servers: z
    .array(
      z.looseObject({
        name: z.string(),
        permission: z.enum(["always_allow", "always_ask"]).optional(),
      })
    )
    .optional(),
  // Cross-session memory. When present, deploy provisions one memory store
  // for this agent (one agent per customer ⇒ one store per customer) and
  // every session attaches it, mounted under /mnt/memory/. `description`
  // and `instructions` are shown to the agent; access defaults read_write —
  // prefer read_only when the agent processes untrusted input.
  memory: z
    .looseObject({
      access: z.enum(["read_write", "read_only"]).optional(),
      description: z.string().optional(),
      instructions: z.string().optional(),
    })
    .optional(),
  // Model for the managed agent, e.g. "claude-sonnet-5".
  model: z.string(),
  name: z.string(),
  // One line per shared-runtime fix made during the compile session.
  runtime_notes: z.array(z.string()).optional(),
  // "reuse" = one MA session per caller session; "fresh" = new session per task.
  session_policy: z.enum(["reuse", "fresh"]),
  // Vault IDs holding MCP credentials; attached to every session at create.
  vault_ids: z.array(z.string()).optional(),
});
export type AgentManifest = z.infer<typeof AgentManifest>;

export type RunTaskOptions = {
  client?: Anthropic;
  manifest: AgentManifest;
  /** Observe every stream event (CLI rendering, transcript capture). */
  onEvent?: (event: SessionEvent) => void;
  /** Outcome mode: rubric markdown (from the agent's rubric.md). */
  rubric?: string;
  /** Reuse an existing session (session_policy "reuse"); omit to create one. */
  sessionId?: string;
  /** The task text: a user message, or the outcome description in outcome mode. */
  task: string;
  /** Hard cap on one task, in milliseconds. Default 10 minutes. */
  timeoutMs?: number;
  /**
   * Abort if the agent has not called an MCP tool within this window.
   *
   * A total timeout only tells you the run was slow. This tells you it never
   * reached its tools at all -- a dead MCP server, an expired credential, a
   * misconfigured toolset -- which is a different failure and worth failing
   * fast on instead of burning the whole budget waiting. Set 0 to disable.
   */
  mcpSilenceMs?: number;
  tools?: CustomToolSpec[];
};

export type RunTaskResult = {
  /** Outcome mode: the grader's final result, e.g. "satisfied". */
  outcome?: { result: string; explanation?: string };
  /** Session ID — stash it to reuse the session for follow-up tasks. */
  sessionId: string;
  /** Final agent message text (or the last one before end-turn idle). */
  text: string;
};

/**
 * A progress snapshot yielded by `streamTask` while the managed agent works.
 * Snapshots are cumulative (each replaces the previous), matching eve's
 * last-write-wins `action.partial` semantics for generator tools.
 */
export type TaskProgress = {
  /** What just happened, e.g. `agent message` or `custom tool: pocket_scan`. */
  activity: string;
  /** Latest agent message text seen so far (empty until the first message). */
  message: string;
  sessionId: string;
};

// ---------------------------------------------------------------------------
// Session stream events
// ---------------------------------------------------------------------------

const SessionEvent = z.looseObject({
  id: z.string().optional(),
  type: z.string(),
});

/** Loosely-typed session stream event (the SDK streams these as unions). */
export type SessionEvent = z.infer<typeof SessionEvent>;

/**
 * The subset of stream events the loop acts on. Everything else (and any
 * event missing a field required here, e.g. a tool_use without an id) is
 * observed via `onEvent` but otherwise ignored.
 */
const KnownEvent = z.discriminatedUnion("type", [
  z.looseObject({
    content: z
      .array(z.looseObject({ text: z.string().optional(), type: z.string() }))
      .default([]),
    type: z.literal("agent.message"),
  }),
  z.looseObject({
    id: z.string(),
    input: z.record(z.string(), z.unknown()).default({}),
    name: z.string(),
    type: z.literal("agent.custom_tool_use"),
  }),
  z.looseObject({
    evaluated_permission: z.string().optional(),
    id: z.string(),
    type: z.enum(["agent.tool_use", "agent.mcp_tool_use"]),
  }),
  z.looseObject({
    explanation: z.string().optional(),
    result: z.string().default(""),
    type: z.literal("span.outcome_evaluation_end"),
  }),
  z.looseObject({
    stop_reason: z
      .looseObject({
        event_ids: z.array(z.string()).default([]),
        type: z.string(),
      })
      .optional(),
    type: z.literal("session.status_idle"),
  }),
  z.looseObject({
    error: z.unknown().optional(),
    type: z.literal("session.status_terminated"),
  }),
]);
type KnownEvent = z.infer<typeof KnownEvent>;

// ---------------------------------------------------------------------------
// Client + environment
// ---------------------------------------------------------------------------

export function makeClient(): Anthropic {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    throw new Error("ANTHROPIC_API_KEY is not set (see .env.example)");
  }
  return new Anthropic({ apiKey });
}

const SHARED_ENVIRONMENT_NAME = "mvp-shared";
let cachedEnvironmentId: string | undefined;

/**
 * Sessions need an environment (the sandbox config). The starter uses one
 * shared cloud environment, found by name or created on first use.
 */
export async function getOrCreateEnvironment(
  client: Anthropic
): Promise<string> {
  if (cachedEnvironmentId) {
    return cachedEnvironmentId;
  }
  for await (const env of client.beta.environments.list()) {
    if (env.name === SHARED_ENVIRONMENT_NAME && env.archived_at === null) {
      cachedEnvironmentId = env.id;
      return env.id;
    }
  }
  const created = await client.beta.environments.create({
    name: SHARED_ENVIRONMENT_NAME,
  });
  cachedEnvironmentId = created.id;
  return created.id;
}

/**
 * Session titles reject Unicode control/format characters (e.g. newlines).
 * Task text is often a pasted document, so collapse all whitespace/control
 * characters before truncating rather than slicing the raw string — a raw
 * slice can land mid-document and carry a newline straight into the title.
 */
function titleSnippet(task: string, maxLen = 60): string {
  const collapsed = task
    .replace(/[\p{Cc}\p{Cf}]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
  return collapsed.length > maxLen
    ? `${collapsed.slice(0, maxLen - 1)}…`
    : collapsed;
}

// ---------------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------------

const DEFAULT_TIMEOUT_MS = 10 * 60 * 1000;
// If an agent has not touched an MCP tool in this long it is not slow -- it
// never got to its tools at all. Fail fast and name the likely causes.
const DEFAULT_MCP_SILENCE_MS = 120_000;

/**
 * Run one task against a deployed managed agent and wait for the result.
 * Thin consumer of `streamTask` for callers that don't need progress.
 */
export async function runTask(opts: RunTaskOptions): Promise<RunTaskResult> {
  const run = streamTask(opts);
  for (;;) {
    // biome-ignore lint/performance/noAwaitInLoops: draining a generator is inherently sequential
    const next = await run.next();
    if (next.done) {
      return next.value;
    }
  }
}

/**
 * Run one task against a deployed managed agent, yielding a `TaskProgress`
 * snapshot as the agent works; the generator's return value is the final
 * `RunTaskResult`. This is what the eve tool wrappers consume from their
 * `async *execute` so intermediate results stream to clients as
 * `action.partial` events.
 */
export async function* streamTask(
  opts: RunTaskOptions
): AsyncGenerator<TaskProgress, RunTaskResult> {
  const client = opts.client ?? makeClient();
  const { deployment } = opts.manifest;
  if (!deployment?.agent_id) {
    throw new Error(
      `"${opts.manifest.name}" has no deployment.agent_id — run: bun run deploy ${opts.manifest.name}`
    );
  }

  let { sessionId } = opts;
  if (!sessionId) {
    const environmentId = await getOrCreateEnvironment(client);
    const session = await client.beta.sessions.create({
      agent: deployment.agent_id,
      environment_id: environmentId,
      title: `${opts.manifest.name}: ${titleSnippet(opts.task)}`,
      ...(opts.manifest.vault_ids?.length
        ? { vault_ids: opts.manifest.vault_ids }
        : {}),
      // Memory stores attach at session create only (not addable later).
      ...(deployment.memory_store_id
        ? {
            resources: [
              {
                access: opts.manifest.memory?.access ?? "read_write",
                instructions: opts.manifest.memory?.instructions,
                memory_store_id: deployment.memory_store_id,
                type: "memory_store" as const,
              },
            ],
          }
        : {}),
    });
    sessionId = session.id;
  }

  // Kick off the turn before consuming the stream.
  if (opts.manifest.invocation === "outcome") {
    if (!opts.rubric) {
      throw new Error("outcome mode requires a rubric (rubric.md)");
    }
    await client.beta.sessions.events.send(sessionId, {
      events: [
        {
          description: opts.task,
          max_iterations: opts.manifest.max_iterations ?? 3,
          rubric: { content: opts.rubric, type: "text" },
          type: "user.define_outcome",
        },
      ],
    });
  } else {
    await client.beta.sessions.events.send(sessionId, {
      events: [
        { content: [{ text: opts.task, type: "text" }], type: "user.message" },
      ],
    });
  }

  return yield* consumeUntilEndTurn({ client, opts, sessionId });
}

type PendingToolUse = { name: string; input: Record<string, unknown> };

type StreamState = {
  lastMessage: string;
  // Outcome mode: the grader inspects sandbox files, not the reply — so
  // agents commonly emit the real deliverable as one agent.message, then a
  // short wrap-up ("all criteria met...") as the true last message once the
  // grader reports satisfied. Taking strictly the last message clobbers the
  // deliverable with that wrap-up. Track the longest message seen instead;
  // in practice the deliverable is far longer than any status remark.
  // Message mode has no such wrap-up phase, so it keeps last-message semantics.
  longestMessage: string;
  outcome?: RunTaskResult["outcome"];
  // Built-in/MCP tool uses that evaluated to permission "ask" — these block the
  // session until a user.tool_confirmation, not a custom_tool_result. This
  // runtime is headless (no human to ask), so it denies with an explanatory
  // message rather than crashing: permission grants belong in the deployed
  // agent config (mcp_toolset permission_policy "always_allow"), set at
  // compile time by the founder.
  pendingPermissionAsks: Set<string>;
  // agent.custom_tool_use events by event ID, so requires_action can find
  // the tool name + input for each blocking event_id.
  pendingToolUses: Map<string, PendingToolUse>;
};

/**
 * Fail fast when an agent that declares an MCP server never calls one.
 *
 * A total timeout tells you the run was slow; this tells you it never reached
 * its tools, which is a different fault with different causes -- and one worth
 * surfacing in two minutes rather than after the whole budget is gone.
 */
/** Total-run deadline guard. Extracted so the event loop reads as a short list
 * of guards; inlining every check pushed the function past the complexity
 * limit, and raising the limit would hide the next one. */
function assertWithinDeadline(
  deadline: number,
  timeoutMs: number,
  sessionId: string
): void {
  if (Date.now() > deadline) {
    throw new Error(
      `runTask timed out after ${timeoutMs}ms (session ${sessionId})`
    );
  }
}

type McpWatch = { deadline: number | null; seen: boolean; silenceMs: number };

/**
 * Track MCP activity and fail fast when an agent that declares an MCP server
 * never calls one.
 *
 * A total timeout tells you the run was slow; this tells you it never reached
 * its tools, which is a different fault with different causes -- and one worth
 * surfacing in two minutes rather than after the whole budget is gone.
 */
function observeMcp(w: McpWatch, eventType: string, sessionId: string): void {
  if (eventType === "agent.mcp_tool_use") {
    w.seen = true;
  }
  if (w.deadline === null || w.seen || Date.now() <= w.deadline) {
    return;
  }
  throw new Error(
    `no MCP tool call within ${w.silenceMs}ms (session ${sessionId}). The agent never reached its MCP tools — check the server is reachable, its credential is valid and unexpired, and the manifest declares an mcp_toolset with permission always_allow.`
  );
}

async function* consumeUntilEndTurn(args: {
  client: Anthropic;
  sessionId: string;
  opts: RunTaskOptions;
}): AsyncGenerator<TaskProgress, RunTaskResult> {
  const { client, sessionId, opts } = args;
  const toolsByName = new Map((opts.tools ?? []).map((t) => [t.name, t]));
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const deadline = Date.now() + timeoutMs;
  // Only watch agents that actually declare an MCP server -- otherwise an
  // agent with no MCP at all would trip the watchdog every time.
  const expectsMcp = (opts.manifest.mcp_servers ?? []).length > 0;
  const mcpSilenceMs = expectsMcp
    ? (opts.mcpSilenceMs ?? DEFAULT_MCP_SILENCE_MS)
    : 0;
  const mcpWatch: McpWatch = {
    deadline: mcpSilenceMs > 0 ? Date.now() + mcpSilenceMs : null,
    seen: false,
    silenceMs: mcpSilenceMs,
  };
  const state: StreamState = {
    lastMessage: "",
    longestMessage: "",
    pendingPermissionAsks: new Set(),
    pendingToolUses: new Map(),
  };

  const stream = await client.beta.sessions.events.stream(sessionId);

  for await (const raw of stream) {
    assertWithinDeadline(deadline, timeoutMs, sessionId);
    opts.onEvent?.(SessionEvent.parse(raw));

    const parsed = KnownEvent.safeParse(raw);
    if (!parsed.success) {
      continue;
    }
    const event = parsed.data;

    observeMcp(mcpWatch, event.type, sessionId);

    if (event.type === "session.status_terminated") {
      throw new Error(
        `session ${sessionId} terminated: ${JSON.stringify(event.error ?? "")}`
      );
    }
    if (event.type !== "session.status_idle") {
      trackEvent(event, state);
      const activity = activityFor(event);
      if (activity) {
        yield { activity, message: state.lastMessage, sessionId };
      }
      continue;
    }
    if (event.stop_reason?.type !== "requires_action") {
      // end_turn (or anything else terminal-ish): we're done.
      return {
        outcome: state.outcome,
        sessionId,
        text: finalText(opts, state),
      };
    }
    await answerBlockingEvents({
      client,
      eventIds: event.stop_reason.event_ids,
      sessionId,
      state,
      toolsByName,
    });
    // session resumes; keep streaming
  }

  return { outcome: state.outcome, sessionId, text: finalText(opts, state) };
}

/** Human-readable label for a progress snapshot; undefined = nothing to show. */
function activityFor(event: KnownEvent): string | undefined {
  switch (event.type) {
    case "agent.message":
      return "agent message";
    case "agent.custom_tool_use":
      return `custom tool: ${event.name}`;
    case "agent.tool_use":
    case "agent.mcp_tool_use":
      return "tool use";
    case "span.outcome_evaluation_end":
      return `outcome evaluated: ${event.result}`;
    default:
      return;
  }
}

function trackEvent(event: KnownEvent, state: StreamState): void {
  switch (event.type) {
    case "agent.message": {
      const text = event.content
        .flatMap((block) =>
          block.type === "text" && block.text ? [block.text] : []
        )
        .join("\n");
      if (text) {
        state.lastMessage = text;
        if (text.length > state.longestMessage.length) {
          state.longestMessage = text;
        }
      }
      break;
    }
    case "agent.custom_tool_use":
      state.pendingToolUses.set(event.id, {
        input: event.input,
        name: event.name,
      });
      break;
    case "agent.tool_use":
    case "agent.mcp_tool_use":
      if (event.evaluated_permission === "ask") {
        state.pendingPermissionAsks.add(event.id);
      }
      break;
    case "span.outcome_evaluation_end":
      state.outcome = { explanation: event.explanation, result: event.result };
      break;
    default:
      break;
  }
}

/** Answer every event blocking a requires_action idle in one batched send. */
async function answerBlockingEvents(args: {
  client: Anthropic;
  eventIds: string[];
  sessionId: string;
  state: StreamState;
  toolsByName: Map<string, CustomToolSpec>;
}): Promise<void> {
  const { client, eventIds, sessionId, state, toolsByName } = args;
  const events = await Promise.all(
    eventIds.map((eventId) => answerFor(eventId, state, toolsByName))
  );
  if (events.length > 0) {
    await client.beta.sessions.events.send(sessionId, { events });
  }
}

async function answerFor(
  eventId: string,
  state: StreamState,
  toolsByName: Map<string, CustomToolSpec>
) {
  if (state.pendingPermissionAsks.has(eventId)) {
    state.pendingPermissionAsks.delete(eventId);
    return {
      deny_message:
        "This caller is headless and cannot approve tool permissions. " +
        "Proceed without this tool; if it is essential, the agent's " +
        "toolset needs permission_policy always_allow at deploy time.",
      result: "deny" as const,
      tool_use_id: eventId,
      type: "user.tool_confirmation" as const,
    };
  }
  const use = state.pendingToolUses.get(eventId);
  state.pendingToolUses.delete(eventId);
  const result = await executeCustomTool(toolsByName, eventId, use);
  return {
    content: [{ text: result, type: "text" as const }],
    custom_tool_use_id: eventId,
    type: "user.custom_tool_result" as const,
  };
}

function finalText(opts: RunTaskOptions, state: StreamState): string {
  return opts.manifest.invocation === "outcome"
    ? state.longestMessage || state.lastMessage
    : state.lastMessage;
}

async function executeCustomTool(
  toolsByName: Map<string, CustomToolSpec>,
  eventId: string,
  use: PendingToolUse | undefined
): Promise<string> {
  if (!use) {
    return `Error: no agent.custom_tool_use event seen for ${eventId}`;
  }
  const tool = toolsByName.get(use.name);
  if (!tool) {
    return `Error: no local handler registered for custom tool "${use.name}"`;
  }
  try {
    return await tool.handler(use.input);
  } catch (error) {
    return `Error executing ${use.name}: ${error instanceof Error ? error.message : String(error)}`;
  }
}

// ---------------------------------------------------------------------------
// Artifact loading (used by console.ts and the eve wrappers)
// ---------------------------------------------------------------------------

/**
 * The source-tree location works when running from tsx CLIs; when eve bundles
 * this module, import.meta.url points into the build output, so fall back to
 * walking up from cwd to the repo root (the dir holding managed/ + package.json).
 */
function findRepoRoot(): string {
  const fromSource = join(dirname(fileURLToPath(import.meta.url)), "..");
  if (existsSync(join(fromSource, "managed"))) {
    return fromSource;
  }
  let dir = process.cwd();
  for (;;) {
    if (
      existsSync(join(dir, "managed")) &&
      existsSync(join(dir, "package.json"))
    ) {
      return dir;
    }
    const parent = dirname(dir);
    if (parent === dir) {
      return fromSource;
    }
    dir = parent;
  }
}
export const repoRoot = findRepoRoot();

// Load the repo-root .env so the CLIs work right after `cp .env.example .env`.
dotenvx.config({
  ignore: ["MISSING_ENV_FILE"],
  path: join(repoRoot, ".env"),
  quiet: true,
});

export type ManagedAgent = {
  dir: string;
  instructions: string;
  manifest: AgentManifest;
  rubric?: string;
  tools: CustomToolSpec[];
};

/**
 * Load an agent from `managed/<name>/` (manifest, CLAUDE.md, rubric, skills,
 * tools.ts — one dir per agent). The dir lives outside `agent/` on purpose:
 * eve requires every module under `agent/tools/**` to BE a tool, so the
 * agent dir — tools.ts included — sits beyond eve's discovery, and only the
 * thin wrapper `agent/tools/<name>.ts` is authored.
 *
 * `skipToolImport`: the eve tool wrappers import their `tools.ts` statically
 * (so the bundler sees it) and pass handlers via `runTask`; they set this to
 * avoid a runtime dynamic import inside the bundled app. CLIs (bun) leave it
 * unset and get handlers loaded dynamically.
 */
export async function loadManagedAgent(
  name: string,
  opts?: { skipToolImport?: boolean }
): Promise<ManagedAgent> {
  const dir = join(repoRoot, "managed", name);
  const manifestPath = join(dir, "manifest.json");
  if (!existsSync(manifestPath)) {
    throw new Error(
      `no managed agent at managed/${name}/ (missing manifest.json)`
    );
  }
  const parsed = AgentManifest.safeParse(
    JSON.parse(await readFile(manifestPath, "utf8"))
  );
  if (!parsed.success) {
    throw new Error(
      `invalid manifest at managed/${name}/manifest.json:\n${z.prettifyError(parsed.error)}`
    );
  }
  const manifest = parsed.data;
  const instructions = await readFile(join(dir, "CLAUDE.md"), "utf8");
  const rubricPath = join(dir, "rubric.md");
  const rubric = existsSync(rubricPath)
    ? await readFile(rubricPath, "utf8")
    : undefined;
  let tools: CustomToolSpec[] = [];
  const toolsPath = join(dir, "tools.ts");
  if (!opts?.skipToolImport && existsSync(toolsPath)) {
    const mod = (await import(toolsPath)) as { tools?: CustomToolSpec[] };
    tools = mod.tools ?? [];
  }
  return { dir, instructions, manifest, rubric, tools };
}

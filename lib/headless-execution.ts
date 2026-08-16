/** Stable, non-interactive boundary for one evidence-mapper round. */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Ajv2020, type ErrorObject } from "ajv/dist/2020.js";
import {
  loadManagedAgent,
  type RunTaskResult,
  repoRoot,
  runTask,
} from "@/lib/claude-managed-agent.ts";

export const DEFAULT_EXECUTION_TIMEOUT_MS = 20 * 60 * 1000;

export type JsonObject = Record<string, unknown>;

export type CannotCompleteReason =
  | "CREDENTIAL_MISSING"
  | "INVALID_GRAPH"
  | "INVALID_REQUEST"
  | "PAPERCLIP_UNAVAILABLE"
  | "PROVIDER_ERROR"
  | "PROVIDER_TIMEOUT";

export type CannotComplete = {
  message: string;
  reason_code: CannotCompleteReason;
  status: "CANNOT_COMPLETE";
};

type Runner = (task: string, timeoutMs: number) => Promise<RunTaskResult>;

export type HeadlessOptions = {
  runner?: Runner;
  timeoutMs?: number;
};

const TIMEOUT_ERROR = /timed out|timeout/i;
const CREDENTIAL_ERROR =
  /ANTHROPIC_API_KEY|api key|credential.*(?:missing|not set)/i;
const PAPERCLIP_ERROR =
  /no MCP tool call|paperclip|MCP.*(?:unavailable|reachable|credential)/i;

class WallClockTimeout extends Error {
  constructor(timeoutMs: number) {
    super(`provider did not finish within ${timeoutMs}ms`);
    this.name = "WallClockTimeout";
  }
}

const readJson = (path: string): JsonObject =>
  JSON.parse(readFileSync(path, "utf8")) as JsonObject;

const ajv = new Ajv2020({ allErrors: true, strict: false });
ajv.addSchema(
  readJson(join(repoRoot, "schema", "interpretability.schema.json"))
);
const validateGraph = ajv.compile(
  readJson(join(repoRoot, "schema", "graph.schema.json"))
);

function firstErrors(errors: ErrorObject[] | null | undefined): string {
  return (errors ?? [])
    .slice(0, 4)
    .map((error) => `${error.instancePath || "/"} ${error.message ?? ""}`)
    .join("; ");
}

function timeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new WallClockTimeout(timeoutMs)),
      timeoutMs
    );
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error);
      }
    );
  });
}

async function defaultRunner(
  task: string,
  timeoutMs: number
): Promise<RunTaskResult> {
  const { manifest, rubric, tools } = await loadManagedAgent(
    "research-evidence-mapper"
  );
  return runTask({ manifest, rubric, task, timeoutMs, tools });
}

function cannotComplete(
  reasonCode: CannotCompleteReason,
  message: string
): CannotComplete {
  return { message, reason_code: reasonCode, status: "CANNOT_COMPLETE" };
}

function mapProviderError(error: unknown): CannotComplete {
  const message = error instanceof Error ? error.message : String(error);
  if (error instanceof WallClockTimeout || TIMEOUT_ERROR.test(message)) {
    return cannotComplete("PROVIDER_TIMEOUT", message);
  }
  if (CREDENTIAL_ERROR.test(message)) {
    return cannotComplete("CREDENTIAL_MISSING", message);
  }
  if (PAPERCLIP_ERROR.test(message)) {
    return cannotComplete("PAPERCLIP_UNAVAILABLE", message);
  }
  return cannotComplete("PROVIDER_ERROR", message);
}

/**
 * Run one request. Successful output is the mapper's exact graph, with no
 * wrapper or projection. Only failures outside the graph contract use the
 * CANNOT_COMPLETE envelope.
 */
export async function executeHeadless(
  request: unknown,
  options: HeadlessOptions = {}
): Promise<JsonObject | CannotComplete> {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    return cannotComplete("INVALID_REQUEST", "input must be one JSON object");
  }
  const timeoutMs = options.timeoutMs ?? DEFAULT_EXECUTION_TIMEOUT_MS;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    return cannotComplete(
      "INVALID_REQUEST",
      "timeout-ms must be a positive number"
    );
  }

  let result: RunTaskResult;
  try {
    const runner = options.runner ?? defaultRunner;
    result = await timeout(
      runner(JSON.stringify(request), timeoutMs),
      timeoutMs
    );
  } catch (error) {
    return mapProviderError(error);
  }

  let graph: JsonObject;
  try {
    const parsed = JSON.parse(result.text) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("agent reply is not one JSON object");
    }
    graph = parsed as JsonObject;
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return cannotComplete(
      "INVALID_GRAPH",
      `agent reply is not valid JSON: ${detail}`
    );
  }

  if (!validateGraph(graph)) {
    return cannotComplete(
      "INVALID_GRAPH",
      `agent graph does not satisfy graph.schema.json: ${firstErrors(validateGraph.errors)}`
    );
  }
  return graph;
}

export function isCannotComplete(
  value: JsonObject | CannotComplete
): value is CannotComplete {
  return value.status === "CANNOT_COMPLETE";
}

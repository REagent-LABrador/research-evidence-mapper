import { describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Ajv2020 } from "ajv/dist/2020.js";
import { type RunTaskResult, repoRoot } from "@/lib/claude-managed-agent.ts";
import {
  executeHeadless,
  isCannotComplete,
  type JsonObject,
} from "@/lib/headless-execution.ts";

const result = (text: string): RunTaskResult => ({
  sessionId: "session-test",
  text,
});
const fixture = (): JsonObject =>
  JSON.parse(
    readFileSync(join(repoRoot, "runs", "g_minimal.json"), "utf8")
  ) as JsonObject;

describe("headless evidence mapper", () => {
  test("the native graph conforms to the pinned shared interpretability contract", () => {
    const contractPath = join(
      repoRoot,
      "contracts",
      "interpretability.schema.json"
    );
    const bytes = readFileSync(contractPath);
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(
      "ac7b27908688851b4fc3de5e3d31642a6e9d4422b422f57161f2c9ab42c3d6bb"
    );
    const schema = JSON.parse(bytes.toString("utf8"));
    const validate = new Ajv2020({ allErrors: true, strict: false }).compile(
      schema
    );
    expect(validate(fixture().interpretability)).toBe(true);
  });

  test("returns the exact valid graph without an integration wrapper", async () => {
    const graph = fixture();
    const output = await executeHeadless(
      { ask: "new_question", target: "does A affect B?" },
      { runner: async () => result(JSON.stringify(graph)), timeoutMs: 100 }
    );
    expect(output).toEqual(graph);
    expect(isCannotComplete(output)).toBe(false);
  });

  test("rejects prose or malformed JSON as INVALID_GRAPH", async () => {
    const output = await executeHeadless(
      { target: "question" },
      { runner: async () => result("```json\n{}\n```"), timeoutMs: 100 }
    );
    expect(output).toMatchObject({
      reason_code: "INVALID_GRAPH",
      status: "CANNOT_COMPLETE",
    });
  });

  test("rejects structurally incomplete graph JSON", async () => {
    const output = await executeHeadless(
      { target: "question" },
      { runner: async () => result('{"graph_id":"g"}'), timeoutMs: 100 }
    );
    expect(output).toMatchObject({
      reason_code: "INVALID_GRAPH",
      status: "CANNOT_COMPLETE",
    });
  });

  test("maps missing credentials", async () => {
    const output = await executeHeadless(
      { target: "question" },
      {
        runner: () => Promise.reject(new Error("ANTHROPIC_API_KEY is not set")),
        timeoutMs: 100,
      }
    );
    expect(output).toMatchObject({
      reason_code: "CREDENTIAL_MISSING",
      status: "CANNOT_COMPLETE",
    });
  });

  test("maps the MCP watchdog to PAPERCLIP_UNAVAILABLE", async () => {
    const output = await executeHeadless(
      { target: "question" },
      {
        runner: () =>
          Promise.reject(new Error("no MCP tool call within 120000ms")),
        timeoutMs: 100,
      }
    );
    expect(output).toMatchObject({
      reason_code: "PAPERCLIP_UNAVAILABLE",
      status: "CANNOT_COMPLETE",
    });
  });

  test("enforces an actual wall-clock timeout on a silent runner", async () => {
    const started = Date.now();
    const output = await executeHeadless(
      { target: "question" },
      {
        runner: () => new Promise<RunTaskResult>(() => undefined),
        timeoutMs: 10,
      }
    );
    expect(Date.now() - started).toBeLessThan(250);
    expect(output).toMatchObject({
      reason_code: "PROVIDER_TIMEOUT",
      status: "CANNOT_COMPLETE",
    });
  });
});

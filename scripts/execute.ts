/**
 * Run one deployed evidence-mapper round without a Console session.
 *
 *   bun run execute -- --input request.json --output graph.json --timeout-ms 1200000
 */

import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import {
  DEFAULT_EXECUTION_TIMEOUT_MS,
  executeHeadless,
  isCannotComplete,
} from "@/lib/headless-execution.ts";

function valueAfter(argv: string[], flag: string): string | undefined {
  const index = argv.indexOf(flag);
  return index === -1 ? undefined : argv[index + 1];
}

function usage(message?: string): never {
  if (message) {
    console.error(message);
  }
  console.error(
    "usage: bun run execute -- --input REQUEST.json --output RESULT.json " +
      "[--timeout-ms 1200000]"
  );
  process.exit(2);
}

const args = process.argv.slice(2).filter((arg) => arg !== "--");
const input = valueAfter(args, "--input");
const output = valueAfter(args, "--output");
const rawTimeout = valueAfter(args, "--timeout-ms");
if (!(input && output)) {
  usage("--input and --output are required");
}
const timeoutMs = rawTimeout
  ? Number(rawTimeout)
  : DEFAULT_EXECUTION_TIMEOUT_MS;
if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
  usage("--timeout-ms must be a positive number");
}

let request: unknown;
try {
  request = JSON.parse(await readFile(resolve(input), "utf8")) as unknown;
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  const terminal = {
    message: `cannot read input JSON: ${message}`,
    reason_code: "INVALID_REQUEST",
    status: "CANNOT_COMPLETE",
  };
  await writeFile(resolve(output), `${JSON.stringify(terminal, null, 2)}\n`);
  console.error(`${terminal.reason_code}: ${terminal.message}`);
  process.exit(2);
}

const result = await executeHeadless(request, { timeoutMs });
await writeFile(resolve(output), `${JSON.stringify(result, null, 2)}\n`);
if (isCannotComplete(result)) {
  console.error(`${result.reason_code}: ${result.message}`);
  // A raced provider promise may still hold an open socket. Exit explicitly so
  // --timeout-ms remains a wall-clock bound for the headless process.
  process.exit(2);
}
process.exit(0);

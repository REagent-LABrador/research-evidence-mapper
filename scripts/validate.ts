/**
 * Validate graph output against the published JSON Schema.
 *
 *   bun run validate                 # the checked-in example, runs/g_e087.json
 *   bun run validate path/to/g.json  # any graph, e.g. one just assembled
 *
 * Three passes, because a schema alone cannot express the parts that matter:
 *   1. Draft 2020-12 validation against schema/graph.schema.json
 *   2. reference integrity  — ids unique, every reference resolves, numeric
 *      metrics carry units, empty arrays are explained by a limitation
 *   3. negative tests       — the contract has to REJECT the things it says it
 *      rejects, or "it validates" means nothing
 *
 * Exits non-zero on any failure.
 */

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Ajv2020, type ErrorObject } from "ajv/dist/2020.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const schemaDir = join(root, "schema");

type Json = Record<string, unknown>;

const NON_JSON_NUMBER = /(^|[\s,:[])(-?Infinity|NaN)([\s,\]}]|$)/;

const readJson = (path: string): Json =>
  JSON.parse(readFileSync(path, "utf8")) as Json;

const ajv = new Ajv2020({ allErrors: true, strict: false });
ajv.addSchema(readJson(join(schemaDir, "interpretability.schema.json")));
const validateGraph = ajv.compile(
  readJson(join(schemaDir, "graph.schema.json"))
);

let failures = 0;

const check = (name: string, ok: boolean, detail?: string) => {
  if (!ok) {
    failures += 1;
  }
  console.log(`${name.padEnd(58)} ${ok ? "PASS" : "FAIL"}`);
  if (!ok && detail) {
    console.log(`    ${detail}`);
  }
};

const firstErrors = (errs: ErrorObject[] | null | undefined, n = 4): string =>
  (errs ?? [])
    .slice(0, n)
    .map((e) => `${e.instancePath || "/"} ${e.message ?? ""}`)
    .join("; ");

/** Deep-clone through JSON so a negative test cannot mutate the original. */
const clone = (value: Json): Json => JSON.parse(JSON.stringify(value)) as Json;

const idsOf = (rows: unknown): string[] =>
  Array.isArray(rows)
    ? rows.map((r) => String((r as Json).id ?? "")).filter(Boolean)
    : [];

const duplicates = (values: string[]): string[] => {
  const seen = new Set<string>();
  const dupes = new Set<string>();
  for (const v of values) {
    if (seen.has(v)) {
      dupes.add(v);
    }
    seen.add(v);
  }
  return [...dupes];
};

type Collections = {
  metrics: Json[];
  steps: Json[];
  evidence: Json[];
  assumptions: Json[];
  limitations: Json[];
  counterfactuals: Json[];
  lineage: Json[];
  uncertainty: Json;
};

const collect = (interp: Json): Collections => ({
  assumptions: (interp.assumptions ?? []) as Json[],
  counterfactuals: (interp.counterfactuals ?? []) as Json[],
  evidence: (interp.evidence ?? []) as Json[],
  limitations: (interp.limitations ?? []) as Json[],
  lineage: (interp.lineage ?? []) as Json[],
  metrics: (interp.metrics ?? []) as Json[],
  steps: (interp.steps ?? []) as Json[],
  uncertainty: (interp.uncertainty ?? {}) as Json,
});

/** "Every ID must be unique within its collection." */
function checkUniqueIds(c: Collections, label: string) {
  for (const [name, rows] of Object.entries({
    assumptions: c.assumptions,
    evidence: c.evidence,
    metrics: c.metrics,
    steps: c.steps,
  })) {
    const dupes = duplicates(idsOf(rows));
    check(`${label}: ${name} ids unique`, dupes.length === 0, dupes.join(", "));
  }
}

/** "Every evidence_id, assumption_id and metric_id reference must resolve." */
function checkReferences(c: Collections, label: string) {
  const evidenceIds = new Set(idsOf(c.evidence));
  const assumptionIds = new Set(idsOf(c.assumptions));
  const metricIds = new Set(idsOf(c.metrics));
  const dangling: string[] = [];

  for (const row of [...c.metrics, ...c.steps]) {
    for (const id of (row.evidence_ids ?? []) as string[]) {
      if (!evidenceIds.has(id)) {
        dangling.push(`${row.id} -> ${id}`);
      }
    }
    for (const id of (row.assumption_ids ?? []) as string[]) {
      if (!assumptionIds.has(id)) {
        dangling.push(`${row.id} -> ${id}`);
      }
    }
  }
  for (const iv of (c.uncertainty.intervals ?? []) as Json[]) {
    if (!metricIds.has(String(iv.metric_id))) {
      dangling.push(`interval -> ${iv.metric_id}`);
    }
  }
  check(
    `${label}: every reference resolves`,
    dangling.length === 0,
    dangling.slice(0, 5).join(", ")
  );
}

/** Units on numerics, and an UNTAGGED_VALUE for anything left unlinked. */
function checkMetricHygiene(c: Collections, label: string) {
  const unitless = c.metrics
    .filter((m) => typeof m.value === "number" && !m.unit)
    .map((m) => String(m.id));
  check(
    `${label}: numeric metrics carry a unit`,
    unitless.length === 0,
    unitless.join(", ")
  );

  const untagged = c.metrics
    .filter(
      (m) =>
        ((m.evidence_ids ?? []) as string[]).length === 0 &&
        ((m.assumption_ids ?? []) as string[]).length === 0
    )
    .map((m) => String(m.id));
  const declared = c.limitations.some((l) => l.code === "UNTAGGED_VALUE");
  check(
    `${label}: untagged metrics declare UNTAGGED_VALUE`,
    untagged.length === 0 || declared,
    untagged.join(", ")
  );
}

/**
 * "Arrays may be empty only when genuinely not applicable, and a limitation
 * must explain the absence." The convention is EMPTY_<COLLECTION>.
 */
function checkEmptyArrays(c: Collections, label: string) {
  check(`${label}: limitations is not empty`, c.limitations.length > 0);
  const codes = new Set(c.limitations.map((l) => String(l.code)));
  const unexplained = Object.entries({
    ASSUMPTIONS: c.assumptions,
    COUNTERFACTUALS: c.counterfactuals,
    EVIDENCE: c.evidence,
    LINEAGE: c.lineage,
    METRICS: c.metrics,
    STEPS: c.steps,
  })
    .filter(([name, rows]) => rows.length === 0 && !codes.has(`EMPTY_${name}`))
    .map(([name]) => name);
  check(
    `${label}: empty arrays are explained`,
    unexplained.length === 0,
    unexplained.join(", ")
  );
}

/**
 * Graph-level reference integrity. The interpretability block is not the only
 * place ids have to resolve: a finding's `from`/`to`/`paper` and a link's
 * `yes`/`no`/`no_effect` name rows too, and nothing enforced that. This header
 * used to advertise "every reference resolves" while checking only the block.
 */
function graphReferences(graph: Json, label: string) {
  const idSet = (rows: unknown) => new Set(idsOf(rows));
  const things = idSet(graph.things);
  const papers = idSet(graph.papers);
  const findings = idSet(graph.findings);
  const dangling: string[] = [];

  const want = (id: unknown, pool: Set<string>, where: string) => {
    if (id !== null && id !== undefined && !pool.has(String(id))) {
      dangling.push(`${where} -> ${id}`);
    }
  };

  for (const f of (graph.findings ?? []) as Json[]) {
    want(f.from, things, `finding ${f.id}.from`);
    want(f.to, things, `finding ${f.id}.to`);
    want(f.paper, papers, `finding ${f.id}.paper`);
  }
  for (const l of (graph.links ?? []) as Json[]) {
    want(l.from, things, `link ${l.id}.from`);
    want(l.to, things, `link ${l.id}.to`);
    for (const axis of ["yes", "no", "no_effect"]) {
      for (const id of (l[axis] ?? []) as string[]) {
        want(id, findings, `link ${l.id}.${axis}`);
      }
    }
  }
  for (const g of (graph.gaps ?? []) as Json[]) {
    for (const id of (g.missing ?? []) as string[]) {
      want(id, things, `gap ${g.id}.missing`);
    }
  }
  check(
    `${label}: graph ids resolve`,
    dangling.length === 0,
    dangling.slice(0, 5).join(", ")
  );
}

function referenceIntegrity(graph: Json, label: string) {
  const interp = graph.interpretability as Json | undefined;
  if (!interp) {
    check(`${label}: interpretability present`, false);
    return;
  }
  graphReferences(graph, label);
  const c = collect(interp);
  checkUniqueIds(c, label);
  checkReferences(c, label);
  checkMetricHygiene(c, label);
  checkEmptyArrays(c, label);
}

function validateFile(path: string) {
  const raw = readFileSync(path, "utf8");
  const label = path.replace(`${root}/`, "");

  // JSON.parse accepts neither NaN nor Infinity, but Python's json.dumps emits
  // them as bare tokens unless allow_nan=False. Catch it in the text, because
  // by the time parse throws the message says nothing useful.
  check(`${label}: no NaN/Infinity tokens`, !NON_JSON_NUMBER.test(raw));

  const graph = JSON.parse(raw) as Json;
  const ok = validateGraph(graph);
  check(
    `${label}: validates against graph.schema.json`,
    ok as boolean,
    firstErrors(validateGraph.errors)
  );
  referenceIntegrity(graph, label);

  // Negative tests, run against this very file so they cannot drift from it.
  const { interpretability: _dropped, ...stripped } = clone(graph);
  check(`${label}: FAILS without interpretability`, !validateGraph(stripped));

  check(
    `${label}: FAILS as bare {graph_id}`,
    !validateGraph({ graph_id: graph.graph_id })
  );

  const noUnit = clone(graph);
  // Must be a NUMERIC metric: the rule under test is "every numeric metric
  // carries a unit", so blanking a string-valued metric's unit proves nothing
  // and the schema correctly still passes — which this test would then report
  // as a failure of the schema.
  const numericMetric = (
    ((noUnit.interpretability as Json | undefined)?.metrics ?? []) as Json[]
  ).find((m) => typeof m.value === "number");
  if (numericMetric) {
    numericMetric.unit = null;
    check(
      `${label}: FAILS when a numeric metric loses its unit`,
      !validateGraph(noUnit)
    );
  }

  const badStatus = clone(graph);
  const headline = (badStatus.interpretability as Json | undefined)?.headline as
    | Json
    | undefined;
  if (headline) {
    headline.status = "PROBABLY";
    check(
      `${label}: FAILS on an out-of-enum status`,
      !validateGraph(badStatus)
    );
  }
}

/**
 * The offline example must still be exactly what the assembler produces. A
 * checked-in example that has drifted from its generator documents a contract
 * nothing implements.
 */
function checkExampleReproducible() {
  const name = "runs/g_minimal.json is byte-identical to a fresh assembly";
  try {
    const produced = execFileSync(
      "python3",
      [
        join(root, "skills", "graph-assembly", "assemble.py"),
        "--input",
        join(root, "fixtures", "round-minimal.json"),
        "--out",
        "-",
      ],
      { encoding: "utf8" }
    );
    check(
      name,
      produced === readFileSync(join(root, "runs", "g_minimal.json"), "utf8")
    );
  } catch (err) {
    check(name, false, String(err));
  }
}

/**
 * Every limitation code the assembler can emit must be documented. A code that
 * exists only in the source is a caveat no consumer can look up, and the table
 * in SCHEMA.md is the only place a consumer looks.
 */
function checkLimitationCodesDocumented() {
  const source = readFileSync(
    join(root, "skills", "graph-assembly", "assemble.py"),
    "utf8"
  );
  const doc = readFileSync(join(root, "SCHEMA.md"), "utf8");
  const emitted = new Set(
    [...source.matchAll(/limit\(\s*"([A-Z][A-Z0-9_]*)"/g)].map((m) => m[1])
  );
  for (const extra of ["GRAPH_NOT_FOUND", "ROUND_NOT_SERVED"]) {
    emitted.add(extra);
  }
  const documented = new Set(
    [...doc.matchAll(/^\| `([A-Z][A-Z0-9_]*)` \|/gm)].map((m) => m[1])
  );
  const undocumented = [...emitted].filter((c) => !documented.has(c)).sort();
  check(
    "every limitation code is documented in SCHEMA.md",
    undocumented.length === 0,
    undocumented.join(", ")
  );
}

const targets = process.argv.slice(2);
const files =
  targets.length > 0
    ? targets
    : [join(root, "runs", "g_e087.json"), join(root, "runs", "g_minimal.json")];
for (const f of files) {
  validateFile(resolve(f));
}
if (targets.length === 0) {
  checkExampleReproducible();
  checkLimitationCodesDocumented();
}

console.log(
  failures === 0
    ? "\nall schema checks passed"
    : `\n${failures} schema check(s) FAILED`
);
process.exit(failures === 0 ? 0 : 1);

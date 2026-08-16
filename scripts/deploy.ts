/**
 * Deploy a managed agent to the Claude Managed Agents API.
 *
 *   bun run deploy <name>
 *
 * Thin Anthropic SDK wrapper, idempotent by content hash:
 *   1. zip + upload each .claude/skills/<dir>/ bundle that changed → Skills API
 *   2. create the agent, or update it (new version) if config changed
 *   3. write agent_id / versions / hashes back into manifest.json
 *
 * Re-running with nothing changed is a no-op.
 */

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { toFile } from "@anthropic-ai/sdk";
import {
  type AgentManifest,
  loadManagedAgent,
  makeClient,
} from "@/lib/claude-managed-agent.ts";

const [name] = process.argv.slice(2);
if (!name) {
  console.error("usage: bun run deploy <name>");
  process.exit(1);
}

const { dir, manifest, instructions, tools } = await loadManagedAgent(name);
const client = makeClient();
const mcpServers = manifest.mcp_servers ?? [];
const deployment: NonNullable<AgentManifest["deployment"]> =
  manifest.deployment ?? {
    agent_id: "",
    agent_version: 0,
    skills: {},
    system_hash: "",
    tools_hash: "",
  };

const sha = (text: string | Buffer) =>
  createHash("sha256").update(text).digest("hex");

// --- 1. skills ------------------------------------------------------------

const skillsRoot = join(dir, ".claude", "skills");
const skillDirs: string[] = [];
if (existsSync(skillsRoot)) {
  for (const entry of await readdir(skillsRoot, { withFileTypes: true })) {
    if (
      !(
        entry.isDirectory() &&
        existsSync(join(skillsRoot, entry.name, "SKILL.md"))
      )
    ) {
      console.error(
        `skill ${entry.name}: skipped — only skill dirs (containing SKILL.md) belong under .claude/skills/`
      );
      continue;
    }
    skillDirs.push(entry.name);
  }
}

const skillResults = await Promise.allSettled(
  skillDirs.map((skillDir) => deploySkill(skillDir))
);
const skillFailures = skillResults.flatMap((result, i) =>
  result.status === "rejected"
    ? [{ dir: skillDirs[i], reason: result.reason }]
    : []
);
if (skillFailures.length > 0) {
  // Persist the IDs of skills that did upload, so a re-run versions them
  // instead of creating workspace orphans.
  await persistManifest();
  for (const failure of skillFailures) {
    console.error(`skill ${failure.dir}: failed — ${String(failure.reason)}`);
  }
  process.exit(1);
}

async function deploySkill(skillDir: string): Promise<void> {
  const bundleHash = await hashDir(join(skillsRoot, skillDir));
  const existing = deployment.skills[skillDir];
  if (existing?.hash === bundleHash) {
    console.log(`skill ${skillDir}: unchanged (${existing.skill_id})`);
    return;
  }
  const zipPath = await zipDir(join(skillsRoot, skillDir), skillDir);
  const file = await toFile(await readFile(zipPath), `${skillDir}.zip`, {
    type: "application/zip",
  });
  if (existing?.skill_id) {
    const version = await client.beta.skills.versions.create(
      existing.skill_id,
      { files: [file] }
    );
    deployment.skills[skillDir] = {
      hash: bundleHash,
      skill_id: existing.skill_id,
      version: String(version.version),
    };
    console.log(
      `skill ${skillDir}: new version ${version.version} of ${existing.skill_id}`
    );
  } else {
    const skill = await client.beta.skills.create({ files: [file] });
    deployment.skills[skillDir] = {
      hash: bundleHash,
      skill_id: skill.id,
      version: String(skill.latest_version ?? "latest"),
    };
    console.log(`skill ${skillDir}: created ${skill.id}`);
  }
  await rm(zipPath, { force: true });
}

// drop records for skills dirs that no longer exist
for (const known of Object.keys(deployment.skills)) {
  if (!skillDirs.includes(known)) {
    console.log(
      `skill ${known}: removed from artifact (leaving uploaded skill in workspace)`
    );
    delete deployment.skills[known];
  }
}

// --- 2. agent -------------------------------------------------------------

const agentConfig = {
  description: manifest.description,
  // "permission" is compile-time metadata for the toolset mapping below — the
  // API's mcp_servers entries only accept {type, name, url}.
  mcp_servers: mcpServers.map(
    ({ permission: _permission, ...server }) => server
  ) as never[],
  model: manifest.model,
  name: manifest.name,
  skills: Object.values(deployment.skills).map((skill) => ({
    skill_id: skill.skill_id,
    type: "custom" as const,
    version: "latest",
  })),
  system: instructions,
  tools: [
    { type: "agent_toolset_20260401" as const },
    // Every declared MCP server must be granted via a matching toolset entry.
    // Each server's manifest entry may carry a "permission" field ("always_allow"
    // or "always_ask", default "always_ask") chosen by the founder at compile
    // time; deploy maps it onto the toolset's permission_policy.
    ...mcpServers.map((server) => ({
      default_config: {
        enabled: true,
        permission_policy: {
          type: server.permission ?? ("always_ask" as const),
        },
      },
      mcp_server_name: server.name,
      type: "mcp_toolset" as const,
    })),
    ...tools.map((tool) => ({
      description: tool.description,
      input_schema: tool.input_schema as {
        type: "object";
        [key: string]: unknown;
      },
      name: tool.name,
      type: "custom" as const,
    })),
  ],
};

const systemHash = sha(instructions);
const toolsHash = sha(
  JSON.stringify(agentConfig.tools) + JSON.stringify(agentConfig.skills)
);

if (!deployment.agent_id) {
  const agent = await client.beta.agents.create(agentConfig);
  deployment.agent_id = agent.id;
  deployment.agent_version = agent.version;
  console.log(`agent created: ${agent.id} v${agent.version}`);
} else if (
  deployment.system_hash !== systemHash ||
  deployment.tools_hash !== toolsHash
) {
  const current = await client.beta.agents.retrieve(deployment.agent_id);
  const agent = await client.beta.agents.update(deployment.agent_id, {
    version: current.version,
    ...agentConfig,
  });
  deployment.agent_version = agent.version;
  console.log(`agent updated: ${agent.id} v${agent.version}`);
} else {
  console.log(
    `agent unchanged: ${deployment.agent_id} v${deployment.agent_version}`
  );
}

deployment.system_hash = systemHash;
deployment.tools_hash = toolsHash;

// --- 3. memory ------------------------------------------------------------

// One store per agent (one agent per customer ⇒ one store per customer),
// created once and reused; runTask attaches it at session create. The store's
// name sets its /mnt/memory/<slug> mount path, so it gets the agent's name.
if (manifest.memory) {
  if (deployment.memory_store_id) {
    console.log(`memory store unchanged: ${deployment.memory_store_id}`);
  } else {
    const store = await client.beta.memoryStores.create({
      description: manifest.memory.description,
      name: manifest.name,
    });
    deployment.memory_store_id = store.id;
    console.log(`memory store created: ${store.id}`);
  }
} else if (deployment.memory_store_id) {
  console.log(
    `memory store detached: ${deployment.memory_store_id} (left in workspace with its memories)`
  );
  deployment.memory_store_id = undefined;
}

// --- 4. write back --------------------------------------------------------

await persistManifest();

async function persistManifest(): Promise<void> {
  manifest.deployment = deployment;
  const manifestPath = join(dir, "manifest.json");
  const nextManifest = `${JSON.stringify(manifest, null, 2)}\n`;
  if (nextManifest === (await readFile(manifestPath, "utf8"))) {
    console.log(`manifest unchanged: managed/${name}/manifest.json`);
  } else {
    await writeFile(manifestPath, nextManifest);
    console.log(`manifest updated: managed/${name}/manifest.json`);
  }
}

// --- helpers --------------------------------------------------------------

async function hashDir(root: string): Promise<string> {
  const files = (
    await readdir(root, { recursive: true, withFileTypes: true })
  ).filter((entry) => entry.isFile());
  const entries = await Promise.all(
    files.map(async (entry) => {
      const path = join(entry.parentPath, entry.name);
      return `${relative(root, path)}:${sha(await readFile(path))}`;
    })
  );
  return sha(entries.toSorted(compareCodeUnits).join("\n"));
}

// Same ordering as the default (UTF-16 code unit) sort, so existing bundle
// hashes stay stable.
function compareCodeUnits(a: string, b: string): number {
  if (a < b) {
    return -1;
  }
  if (a > b) {
    return 1;
  }
  return 0;
}

async function zipDir(root: string, dirName: string): Promise<string> {
  const staging = await mkdtemp(join(tmpdir(), "skill-"));
  const zipPath = join(staging, `${dirName}.zip`);
  // Zip so the archive contains <dirName>/SKILL.md at the top level.
  execFileSync("zip", ["-r", zipPath, dirName], {
    cwd: join(root, ".."),
    stdio: "pipe",
  });
  return zipPath;
}

/**
 * Refresh the local Paperclip login and push the token into the agent's vault.
 *
 *   bun run rotate
 *
 * Paperclip authenticates with an OAuth id_token that lives about an hour, and
 * the vault credential the deployed agent uses is a static bearer holding a
 * copy of it. So the copy goes stale on its own, and when it does the platform
 * exposes NO paperclip tool to the agent at all — which reads downstream as
 * "the corpus is empty", not as "the credential expired".
 *
 * Order matters, and getting it wrong is the trap this script exists to close:
 * refresh FIRST, then rotate. Rotating without refreshing copies a token that
 * is already minutes from expiry into the vault and buys nothing, while
 * looking like it worked.
 *
 * Run it immediately before `bun run console`. There is no way to make the
 * stored token outlive its hour; a long-lived Paperclip API key would remove
 * the need for this script entirely.
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { loadManagedAgent, makeClient } from "@/lib/claude-managed-agent.ts";

const CREDS = join(homedir(), ".paperclip", "credentials.json");
const VENV_PY = join(homedir(), ".paperclip", "venv", "bin", "python");
const MIN_MINUTES = 15;

const minutesLeft = (): number => {
  const { id_token } = JSON.parse(readFileSync(CREDS, "utf8")) as {
    id_token: string;
  };
  const [, payload] = id_token.split(".");
  const { exp } = JSON.parse(
    Buffer.from(payload, "base64").toString("utf8")
  ) as { exp: number };
  return Math.round((exp * 1000 - Date.now()) / 60_000);
};

const token = (): string =>
  (JSON.parse(readFileSync(CREDS, "utf8")) as { id_token: string }).id_token;

if (!existsSync(CREDS)) {
  console.error(
    `no Paperclip credentials at ${CREDS} — run \`paperclip login\``
  );
  process.exit(1);
}

console.log(`local token: ${minutesLeft()} min left`);

// Refresh first. The CLI only refreshes lazily, near expiry, so ask the
// provider directly rather than hoping a search triggers it.
if (existsSync(VENV_PY)) {
  execFileSync(
    VENV_PY,
    [
      "-c",
      [
        "import sys, os",
        "sys.path.insert(0, os.path.expanduser('~/.paperclip/lib'))",
        "from gxl_paperclip.client.auth import FileCredentialsAuth",
        "FileCredentialsAuth().refresh()",
      ].join("; "),
    ],
    { stdio: "inherit" }
  );
  console.log(`after refresh: ${minutesLeft()} min left`);
} else {
  console.log(
    `no venv at ${VENV_PY} — skipping refresh, using the token as is`
  );
}

const left = minutesLeft();
if (left < MIN_MINUTES) {
  console.error(
    `refusing: only ${left} min left after refresh. Pushing this into the vault ` +
      "would look like a rotation and expire mid-run. Try `paperclip login`."
  );
  process.exit(1);
}

const { manifest } = await loadManagedAgent();
const [vaultId] = manifest.vault_ids ?? [];
if (!vaultId) {
  console.error("manifest.vault_ids is empty — nothing to rotate");
  process.exit(1);
}

// biome-ignore lint/suspicious/noExplicitAny: vaults are not in the typed surface yet
const client = makeClient() as any;
const { data: credentials } =
  await client.beta.vaults.credentials.list(vaultId);
const target = (credentials ?? []).find(
  (c: { auth?: { type?: string } }) => c.auth?.type === "static_bearer"
);
if (!target) {
  console.error(`vault ${vaultId} has no static_bearer credential`);
  process.exit(1);
}

const updated = await client.beta.vaults.credentials.update(target.id, {
  auth: { token: token(), type: "static_bearer" },
  vault_id: vaultId,
});
console.log(
  `rotated ${updated.id} (${updated.display_name}) at ${updated.updated_at} — good for ~${left} min`
);

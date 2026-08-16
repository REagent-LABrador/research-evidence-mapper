import type { DynamicResolveContext } from "eve/tools";

// Per-caller tool visibility.
//
// This file is the mechanism; each managed agent declares its own rules in
// `managed/<name>/acl.ts` and its wrapper in agent/tools/<name>.ts
// default-exports a defineDynamic resolver that asks `allowed(ctx, acl)`
// before revealing its tool. A `{ public: true }` ACL means every caller sees
// the tool — identical to having no gating at all.
//
// To gate tools per caller after forking this repo (/managed-agent-setup
// walks you through all three):
//
//   1. Wire authentication into the eve router so ctx.session.auth is
//      populated: https://eve.dev/docs/guides/auth-and-route-protection
//   2. Change the agent's managed/<name>/acl.ts to { principals: [...] } — the
//      caller ids (org ids or user ids, your call) that may see its tool.
//   3. If your caller id isn't the principalId your authenticator issues,
//      adjust resolvePrincipal (e.g. read an org id you stamped into
//      ctx.session.auth.current.attributes at login).
//
// How the mechanics work (and their landmines):
// https://eve.dev/docs/guides/dynamic-capabilities
//
//   - eve registers every JS-family module under agent/tools/** as a tool,
//     and a dynamic resolver can only *override* an authored tool by name —
//     never hide it. That is why each wrapper's default export is the
//     defineDynamic resolver itself: a plain defineTool default export under
//     agent/tools/ would stay visible to every caller no matter what any
//     resolver returns.
//   - Inside a resolver, defineTool's `execute` must be an inline function
//     (an arrow calling imports is fine). `execute: importedFn` works on the
//     first step but silently breaks on durable replay.

export type ACL = { public: true } | { principals: string[] };

// TODO(fork): derive your caller id from the authenticated session.
function resolvePrincipal(ctx: DynamicResolveContext): string | undefined {
  return ctx.session.auth.current?.principalId;
}

export function allowed(ctx: DynamicResolveContext, acl: ACL): boolean {
  if ("public" in acl) {
    return true;
  }
  const who = resolvePrincipal(ctx);
  return who ? acl.principals.includes(who) : false;
}

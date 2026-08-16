import type { ACL } from "@/lib/access.ts";

// Who may call this agent through the router. Enforcement starts once auth
// is wired into the eve router (see lib/access.ts).
export const acl: ACL = { public: true };

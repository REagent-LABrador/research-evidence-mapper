import { defineState } from "eve/context";
import { defineDynamic, defineTool } from "eve/tools";
import { allowed } from "@/lib/access.ts";
import { loadManagedAgent, runTask } from "@/lib/claude-managed-agent.ts";
import { acl } from "@/managed/research-evidence-mapper/acl.ts";

const sessionIdState = defineState<string | undefined>(
  "research-evidence-mapper-session",
  () => undefined
);

export default defineDynamic({
  events: {
    "session.started": (_event, ctx) =>
      allowed(ctx, acl)
        ? defineTool({
            description:
              "Turns one question about the scientific literature into a knowledge graph of " +
              "things, verbatim-quoted findings, scored links and gaps, and grows it across " +
              "rounds. Provide a complete, self-contained task; the specialist runs remotely " +
              "and returns its final answer as the full graph JSON.",
            async execute(input) {
              const { manifest, rubric } = await loadManagedAgent(
                "research-evidence-mapper",
                {
                  skipToolImport: true,
                }
              );
              const previous =
                manifest.session_policy === "reuse"
                  ? sessionIdState.get()
                  : undefined;
              const result = await runTask({
                manifest,
                rubric,
                sessionId: previous,
                task: String(input.task),
              });
              if (manifest.session_policy === "reuse") {
                sessionIdState.update(() => result.sessionId);
              }
              return result.text;
            },
            inputSchema: {
              properties: {
                task: {
                  description:
                    'A JSON request: {"ask":"new_question|expand_node|resolve_link|test_gap",' +
                    '"target":"<free text or an id>","depth":"quick|standard|deep|exhaustive",' +
                    '"graph_id":"<omit for new_question>"}. Plain text is treated as a new_question.',
                  type: "string",
                },
              },
              required: ["task"],
              type: "object",
            },
          })
        : null,
  },
});

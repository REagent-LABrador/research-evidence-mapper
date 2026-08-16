import { anthropic } from "@ai-sdk/anthropic";
import { defineAgent } from "eve";

export default defineAgent({
  model: anthropic("claude-opus-5"),
});

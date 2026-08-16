import { experimental_workflow } from "eve/tools";

// Lets the router write and run small orchestration programs over its
// other tools (fan-out, sequencing) instead of one call at a time.
export default experimental_workflow();

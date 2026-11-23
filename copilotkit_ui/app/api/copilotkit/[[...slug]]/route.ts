// CopilotKit runtime: bridges the browser to the ADK AG-UI endpoint (adk-agui container).
// `a2ui.injectA2UITool` makes the runtime/adapter inject `generate_a2ui` so the agent can render A2UI surfaces.
import { CopilotRuntime, createCopilotHonoHandler } from "@copilotkit/runtime/v2";
import { HttpAgent } from "@ag-ui/client";

const runtime = new CopilotRuntime({
  agents: { preauth: new HttpAgent({ url: process.env.AGUI_URL || "http://host.docker.internal:8002/agui" }) },
  a2ui: { injectA2UITool: true },
} as any);

const app = createCopilotHonoHandler({ runtime, basePath: "/api/copilotkit", mode: "single-route" } as any);
const handler = (req: Request) => app.fetch(req);
export const GET = handler, POST = handler, PUT = handler, DELETE = handler, OPTIONS = handler;

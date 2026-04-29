import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { SSEClientTransport } from "@modelcontextprotocol/sdk/client/sse.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";

// Configuration
const REMOTE_URL = "{{BASE_URL}}/mcp";
const AUTH_HEADER = "Basic {{AUTH_BASE64}}"; // Your encoded credentials

const transport = new SSEClientTransport(new URL(REMOTE_URL), {
  eventSourceInitDict: { headers: { "Authorization": AUTH_HEADER, "Accept": "text/event-stream" } },
  requestInit: { headers: { "Authorization": AUTH_HEADER, "Content-Type": "application/json" } }
});
transport.onclose = () => console.log("Transport closed");
transport.onerror = (error) => console.error("Transport error details:", error);

const client = new Client({ name: "bridge-client", version: "1.0.0" }, { capabilities: { sampling: {} } });
await client.connect(transport);

// Map remote tools to local stdio
const server = new Server({ name: "bridge-server", version: "1.0.0" }, { capabilities: { tools: {}, sampling: {} } });
const stdioTransport = new StdioServerTransport();

server.setRequestHandler(Symbol.for("mcp.listTools"), () => client.listTools());
server.setRequestHandler(Symbol.for("mcp.callTool"), (req) => client.callTool(req.params.name, req.params.arguments));

await server.connect(stdioTransport);

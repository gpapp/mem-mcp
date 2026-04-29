import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { SSEClientTransport } from "@modelcontextprotocol/sdk/client/sse.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";

// Configuration
const REMOTE_URL = "{{BASE_URL}}";
const AUTH_HEADER = "{{AUTH_BASE64}}"; // Your encoded credentials

const transport = new SSEClientTransport(new URL(REMOTE_URL), {
  eventSourceInitDict: { headers: { "Authorization": AUTH_HEADER } },
  requestInit: { headers: { "Authorization": AUTH_HEADER } }
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

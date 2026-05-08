import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { HTTPClientTransport } from "@modelcontextprotocol/sdk/client/http.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";

const REMOTE_URL = "{{BASE_URL}}/mcp";
const AUTH_HEADER = "Basic {{AUTH_BASE64}}";

const transport = new HTTPClientTransport(new URL(REMOTE_URL), {
  requestInit: { headers: { "Authorization": AUTH_HEADER, "Content-Type": "application/json" } }
});

const client = new Client({ name: "bridge-client", version: "1.0.0" }, { capabilities: { sampling: {} } });
await client.connect(transport);

const server = new Server({ name: "bridge-server", version: "1.0.0" }, { capabilities: { tools: {}, sampling: {} } });
const stdioTransport = new StdioServerTransport();

server.setRequestHandler(Symbol.for("mcp.listTools"), () => client.listTools());
server.setRequestHandler(Symbol.for("mcp.callTool"), (req) => client.callTool(req.params.name, req.params.arguments));

await server.connect(stdioTransport);
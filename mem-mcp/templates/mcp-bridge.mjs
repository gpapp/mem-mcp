import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { HTTPClientTransport } from "@modelcontextprotocol/sdk/client/http.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";

// The key is read from the environment rather than pasted into this file.
// It used to be baked in here as a base64 Basic header, which meant a downloaded
// script was a plain-text credential: it landed in backups, in `git add .`, in a
// screen share, and in whatever copies of the file existed on every machine the
// bridge was set up on. An env var is at least one place you can point at, and
// one place to revoke from.
//
//   export MEM_VAULT_PSK='mvk_...'
//
// The server URL is still filled in for you, since it is not a secret.
const PSK = (process.env.MEM_VAULT_PSK || "").trim();
if (!PSK) {
  console.error(
    "mcp-bridge: MEM_VAULT_PSK is not set.\n" +
    "  Create a key in the vault under Setup -> Access Keys, then:\n" +
    "    export MEM_VAULT_PSK='mvk_...'\n" +
    "  See Setup -> Local Proxy Bridge for the full command."
  );
  process.exit(1);
}

const REMOTE_URL = "{{BASE_URL}}/mcp";
const AUTH_HEADER = `Bearer ${PSK}`;

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
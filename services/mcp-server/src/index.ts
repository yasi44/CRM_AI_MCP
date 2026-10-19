/**
 * HTTP entry point for the MCP server (Streamable HTTP transport).
 *
 * Per request:  verify JWT -> build a role-aware MCP server -> let the SDK
 * transport handle the JSON-RPC message -> tear everything down.
 */
import express, { type NextFunction, type Request, type Response } from "express";
import { jwtVerify } from "jose";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { buildServer } from "./server.js";
import { CrmClient } from "./crmClient.js";

const PORT = Number(process.env.PORT ?? 8081);
const CRM_API_URL = process.env.CRM_API_URL ?? "http://localhost:8080";
const JWT_SECRET = process.env.JWT_SECRET ?? "";
if (JWT_SECRET.length < 32) {
  console.error("JWT_SECRET must be at least 32 characters");
  process.exit(1);
}
const secretKey = new TextEncoder().encode(JWT_SECRET);

interface AuthedRequest extends Request {
  auth?: { token: string; role: string; email: string };
}

/** Reject early (401) if the bearer token is missing/invalid. The CRM API re-verifies it too. */
async function authenticate(req: AuthedRequest, res: Response, next: NextFunction) {
  const token = req.header("authorization")?.replace(/^Bearer\s+/i, "");
  if (!token) return res.status(401).json({ error: "missing bearer token" });
  try {
    const { payload } = await jwtVerify(token, secretKey, {
      algorithms: ["HS256"], issuer: "crm-api", audience: "crm",
    });
    req.auth = { token, role: String(payload.role), email: String(payload.email) };
    next();
  } catch {
    res.status(401).json({ error: "invalid or expired token" });
  }
}

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "1mb" }));

app.get("/healthz", (_req, res) => res.json({ status: "ok" }));

app.post("/mcp", authenticate, async (req: AuthedRequest, res) => {
  const { token, role, email } = req.auth!;
  const server = buildServer(role, new CrmClient(CRM_API_URL, token));
  // sessionIdGenerator: undefined => STATELESS mode (no server-side session).
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
  res.on("close", () => { transport.close(); server.close(); });
  try {
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
    console.log(JSON.stringify({ level: "info", msg: "mcp request", email, role, method: req.body?.method }));
  } catch (err) {
    console.error(JSON.stringify({ level: "error", msg: "mcp failure", err: String(err) }));
    if (!res.headersSent) res.status(500).json({ jsonrpc: "2.0", error: { code: -32603, message: "Internal error" }, id: null });
  }
});

// Stateless servers don't support server-initiated streams or session teardown.
const notAllowed = (_req: Request, res: Response) =>
  res.status(405).json({ jsonrpc: "2.0", error: { code: -32000, message: "Method not allowed" }, id: null });
app.get("/mcp", notAllowed);
app.delete("/mcp", notAllowed);

const httpServer = app.listen(PORT, () => console.log(`mcp-server listening on :${PORT}`));
process.on("SIGTERM", () => httpServer.close(() => process.exit(0))); // graceful shutdown

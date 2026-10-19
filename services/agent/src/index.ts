/**
 * Agent gateway: the HTTP surface your web/mobile SaaS app would call.
 *   POST /v1/chat                          free-form questions
 *   POST /v1/workflows/stalled-deals       an automated workflow (same engine, fixed task)
 */
import express, { type NextFunction, type Request, type Response } from "express";
import rateLimit from "express-rate-limit";
import { jwtVerify } from "jose";
import { z } from "zod";
import { BASE_SYSTEM, runAgent } from "./agent.js";

const PORT = Number(process.env.PORT ?? 8082);
const JWT_SECRET = process.env.JWT_SECRET ?? "";
if (JWT_SECRET.length < 32) { console.error("JWT_SECRET must be at least 32 characters"); process.exit(1); }
if (!process.env.ANTHROPIC_API_KEY) { console.error("ANTHROPIC_API_KEY is required"); process.exit(1); }
const secretKey = new TextEncoder().encode(JWT_SECRET);

interface AuthedRequest extends Request { auth?: { token: string; role: string; email: string } }

async function authenticate(req: AuthedRequest, res: Response, next: NextFunction) {
  const token = req.header("authorization")?.replace(/^Bearer\s+/i, "");
  if (!token) return res.status(401).json({ error: "missing bearer token" });
  try {
    const { payload } = await jwtVerify(token, secretKey, { algorithms: ["HS256"], issuer: "crm-api", audience: "crm" });
    req.auth = { token, role: String(payload.role), email: String(payload.email) };
    next();
  } catch { res.status(401).json({ error: "invalid or expired token" }); }
}

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "100kb" }));
app.get("/healthz", (_req, res) => res.json({ status: "ok" }));

// LLM calls cost money: throttle hard. (Use a shared store like Redis with many replicas.)
app.use("/v1", rateLimit({ windowMs: 60_000, limit: 20, standardHeaders: true, legacyHeaders: false }));

const ChatBody = z.object({ message: z.string().min(1).max(2000), allow_writes: z.boolean().default(false) });

app.post("/v1/chat", authenticate, async (req: AuthedRequest, res) => {
  const parsed = ChatBody.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid body", details: parsed.error.flatten() });
  try {
    const out = await runAgent({ token: req.auth!.token, prompt: parsed.data.message, allowWrites: parsed.data.allow_writes });
    console.log(JSON.stringify({ level: "info", msg: "chat", email: req.auth!.email, tools: out.toolCalls }));
    res.json(out);
  } catch (e) {
    console.error(JSON.stringify({ level: "error", msg: "agent failed", err: String(e) }));
    res.status(502).json({ error: "agent failed" });
  }
});

/**
 * Intelligent workflow automation: a fixed, repeatable task. In production,
 * trigger this from a scheduler (cron / Cloud Scheduler / EventBridge) using a
 * dedicated low-privilege service user, and push the result to Slack/email.
 */
app.post("/v1/workflows/stalled-deals", authenticate, async (req: AuthedRequest, res) => {
  try {
    const out = await runAgent({
      token: req.auth!.token,
      allowWrites: false, // workflows RECOMMEND; humans approve changes
      system: BASE_SYSTEM,
      prompt: "List opportunities in the Engaging stage. Rank the five most at risk of stalling " +
        "(oldest engage_date first). For each give id, account, agent and one concrete follow-up action. Be concise.",
    });
    res.json(out);
  } catch (e) {
    console.error(JSON.stringify({ level: "error", msg: "workflow failed", err: String(e) }));
    res.status(502).json({ error: "workflow failed" });
  }
});

const server = app.listen(PORT, () => console.log(`agent listening on :${PORT}`));
process.on("SIGTERM", () => server.close(() => process.exit(0)));

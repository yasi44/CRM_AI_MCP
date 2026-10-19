/**
 * Thin HTTP client for the CRM API (Go service).
 *
 * KEY SECURITY IDEA: this client carries the END USER'S token, not a powerful
 * service account. So when an AI model calls an MCP tool, the CRM API still
 * enforces that user's RBAC and still writes that user's name in the audit log.
 * The AI can never do more than the person using it.
 */
export class CrmApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export class CrmClient {
  constructor(private baseUrl: string, private token: string) {}

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        ...(init.headers ?? {}),
        Authorization: `Bearer ${this.token}`,
        "Content-Type": "application/json",
      },
      // Never let a slow backend hang an AI conversation forever.
      signal: AbortSignal.timeout(10_000),
    });
    const text = await res.text();
    if (!res.ok) {
      // Surface the API's error text so the model can explain it ("forbidden: ...").
      throw new CrmApiError(res.status, text.slice(0, 500));
    }
    return JSON.parse(text) as T;
  }
}

/**
 * api.ts — ProcuraAI API Abstraction Layer (Story 5.2)
 *
 * All API calls go through this module.
 * Base URL is driven by VITE_API_URL environment variable.
 *
 * Dev:        VITE_API_URL=http://localhost:8000/api  (set in .env.local)
 * Production: VITE_API_URL=https://procuraai-api.onrender.com/api  (set in Vercel env vars)
 */

const BASE_URL = (import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api').replace(/\/$/, '');

// ─── Types ────────────────────────────────────────────────────────────────────

export interface AuthPayload {
  access_token: string;
  token_type: string;
  user_id: string;
  email?: string;
  full_name?: string;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

// ─── Core fetch helper ────────────────────────────────────────────────────────

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${BASE_URL}${path}`;
  const res = await fetch(url, options);

  if (!res.ok) {
    let msg = `Request failed: ${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) msg = String(body.detail);
    } catch {}
    throw new ApiError(res.status, msg);
  }

  // Handle empty responses (e.g. 204 No Content)
  const text = await res.text();
  return text ? (JSON.parse(text) as T) : ({} as T);
}

function authHeaders(token: string): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${token}`,
  };
}

// ─── Auth ─────────────────────────────────────────────────────────────────────

export const auth = {
  login: (username: string, password: string) =>
    request<AuthPayload>('/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }),

  register: (username: string, password: string, full_name: string) =>
    request<AuthPayload>('/v1/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, full_name }),
    }),
};

// ─── RFQ Automation ───────────────────────────────────────────────────────────

export const rfq = {
  getConversations: (token: string) =>
    request<any[]>('/v1/rfq-auto/conversations', {
      headers: authHeaders(token),
    }),

  syncMailbox: (token: string) =>
    request<{ status: string }>('/v1/rfq-auto/sync-mailbox', {
      method: 'POST',
      headers: authHeaders(token),
    }),

  extractItems: (token: string, conversationId: string) =>
    request<any>('/v1/rfq-auto/extract-items', {
      method: 'POST',
      headers: authHeaders(token),
      body: JSON.stringify({ conversation_id: conversationId }),
    }),

  generateQuote: (token: string, conversationId: string) =>
    request<any>('/v1/rfq-auto/generate-quote', {
      method: 'POST',
      headers: authHeaders(token),
      body: JSON.stringify({ conversation_id: conversationId }),
    }),

  draftEmail: (token: string, conversationId: string) =>
    request<any>('/v1/rfq-auto/draft-email', {
      method: 'POST',
      headers: authHeaders(token),
      body: JSON.stringify({ conversation_id: conversationId }),
    }),

  sendQuote: (token: string, conversationId: string) =>
    request<any>('/v1/rfq-auto/send-quote', {
      method: 'POST',
      headers: authHeaders(token),
      body: JSON.stringify({ conversation_id: conversationId }),
    }),

  /** Returns a full URL (not a fetch call) for PDF download links */
  pdfDownloadUrl: (quoteNumber: string, token: string): string =>
    `${BASE_URL}/v1/rfq-auto/download-pdf/${quoteNumber}?token=${token}`,
};

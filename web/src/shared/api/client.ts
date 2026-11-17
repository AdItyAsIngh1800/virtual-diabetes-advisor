/**
 * The API client: same-origin /api calls with the Supabase session token. Response
 * types come only from the generated OpenAPI file (types.ts, `npm run gen:api`).
 */
import type { components } from "@/shared/api/types";
import { getSupabase } from "@/shared/auth/supabase";

export type S = components["schemas"];

/** An /api call that failed; `message` is the API's `detail` when it sent one. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Turn a failed response into an ApiError carrying the API's detail. */
export async function failure(res: Response): Promise<ApiError> {
  let detail = `The server answered ${res.status}.`;
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object" && "detail" in body) {
      const d = (body as { detail: unknown }).detail;
      detail = typeof d === "string" ? d : JSON.stringify(d);
    }
  } catch {
    /* not JSON */
  }
  return new ApiError(detail, res.status);
}

type Method = "GET" | "POST" | "PUT" | "DELETE";

/**
 * Call one of our /api routes. `body` may be a plain object (sent as JSON) or
 * FormData (multipart, for voice uploads). Resolves to the JSON body, or null for 204.
 */
export async function api<T>(path: string, method: Method = "GET", body?: unknown): Promise<T> {
  const { sb } = await getSupabase();
  const { data } = await sb.auth.getSession();
  const headers: Record<string, string> = {};
  if (data.session) headers.Authorization = `Bearer ${data.session.access_token}`;
  let payload: BodyInit | undefined;
  if (body instanceof FormData) {
    payload = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let res: Response;
  try {
    res = await fetch(path, { method, headers, body: payload });
  } catch {
    throw new ApiError("Could not reach the server. Check your connection and try again.", 0);
  }
  if (!res.ok) throw await failure(res);
  return (res.status === 204 ? null : await res.json()) as T;
}

/** A short, readable message for any thrown value. */
export function errorText(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

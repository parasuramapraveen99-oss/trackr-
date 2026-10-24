/**
 * Thin fetch wrapper. Every error response from app/main.py's exception handlers
 * (trackr-backend/app/main.py:71-75 `_error`, and FastAPI's own HTTPException/422
 * validation errors) is shaped `{ detail: string }` -- ApiError surfaces that string.
 */

/**
 * Where the API lives. The localhost default is a DEVELOPMENT convenience only.
 *
 * It used to apply everywhere, which meant a deployment missing
 * `NEXT_PUBLIC_API_BASE_URL` silently pointed every request at the viewer's own machine
 * and failed as a connection error on each screen, with nothing anywhere naming the
 * cause. A missing base URL in a production build is a deployment fault, so it says so
 * once, plainly, rather than being papered over with a value that cannot be right.
 *
 * `NODE_ENV` is inlined by Next at build time, so this costs nothing at runtime and the
 * dev fallback is not even present in a production bundle.
 */
const DEV_API_BASE_URL = "http://localhost:8000";
const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  (process.env.NODE_ENV === "production" ? "" : DEV_API_BASE_URL);

/** Plus uses its same-origin proxy and an HttpOnly cookie. No credential lives in JS. */
export const BROWSER_SESSION = "cookie-session";
let browserSession = false;
export function configureBrowserSession(): void {
  browserSession = true;
}

export class ApiError extends Error {
  status: number;
  /** Extra fields app/main.py's `_error()` attaches beyond `detail` -- e.g.
   * MissingPriceError's `isins`, so a caller can name them instead of showing a
   * generic failure (docs/BUILD_PLAN.md: "a missing price raises; the UI names the
   * ISINs"). */
  isins?: string[];
  /** LegalHoldError's `reasons` (app/db/privacy.py) -- the open legal-hold reasons
   * blocking erasure, so the privacy page can explain why self-serve delete is
   * refused instead of showing a generic failure toast. */
  reasons?: string[];
  batchId?: string;
  /** NegativeHoldingError's fields (app/engine/errors.py) -- the ISIN whose disposals
   * exceed its acquisitions, how many units short, and the date the shortfall first
   * appeared. Carried so the UI can name the ISIN and point at the exact date instead
   * of showing the raw "buy history is incomplete" string as a dead end. */
  isin?: string;
  deficit?: string;
  asOf?: string;
  /** Best-effort ISIN -> company name, resolved server-side (app.main's `_names_for`)
   * so an error can read naturally instead of showing a bare ISIN. Absent, or missing an
   * entry, when the lookup failed or the ISIN has no security row -- callers fall back
   * to the ISIN in that case. Named `securityName`, not `name` -- `Error.name` is
   * already `"ApiError"`. */
  securityName?: string;
  securityNames?: Record<string, string>;
  /** A stable machine-readable identifier for the refusal, where the backend attaches
   * one -- currently `workspace_capability_required` (app/db/capabilities.py). Clients
   * branch on THIS, never on `message`: the sentence is written for a person and is
   * free to improve, the code is part of the contract. Absent on the many errors that
   * carry no code, so a caller must handle undefined rather than assume one. */
  code?: string;
  /** Workspace research envelope only: input field -> short reason code, so a form can
   * attach an error to the right control. Absent everywhere else. */
  fields?: Record<string, unknown>;
  constructor(
    status: number,
    message: string,
    isins?: string[],
    reasons?: string[],
    batchId?: string,
    isin?: string,
    deficit?: string,
    asOf?: string,
    securityName?: string,
    securityNames?: Record<string, string>,
    code?: string,
    fields?: Record<string, unknown>
  ) {
    super(message);
    this.status = status;
    this.name = "ApiError";
    this.isins = isins;
    this.reasons = reasons;
    this.batchId = batchId;
    this.isin = isin;
    this.deficit = deficit;
    this.asOf = asOf;
    this.securityName = securityName;
    this.securityNames = securityNames;
    this.code = code;
    this.fields = fields;
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  token?: string | null;
  json?: unknown;
  formData?: FormData;
  query?: Record<string, string | number | boolean | undefined | null>;
  /** Extra request headers -- e.g. Workspace research writes' `Idempotency-Key`. Cannot
   * override `Authorization` or `Content-Type`, which this client sets itself. */
  headers?: Record<string, string>;
  /** Internal: set when this call is already the one automatic retry after a
   * successful token refresh, so a second 401 surfaces instead of looping. */
  _retry?: boolean;
}

/**
 * Registered by AuthProvider (lib/auth-context.tsx). When a request comes back 401,
 * the client calls it once for a fresh token -- POST /auth/refresh under the hood --
 * and retries the original request with it. Kept as an injected hook rather than an
 * import so api-client stays dependency-free: auth-context imports this module, never
 * the reverse.
 */
type UnauthorizedHandler = () => Promise<string | null>;
let unauthorizedHandler: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("\\")) {
    throw new Error("Invalid API path.");
  }
  if (!browserSession && !BASE_URL) {
    throw new Error(
      "NEXT_PUBLIC_API_BASE_URL is not set for this build, so there is no API to call. " +
        "Set it in the deployment's environment and redeploy."
    );
  }
  const url = browserSession
    ? new URL(`/api/trackr${path}`, window.location.origin)
    : new URL(path, BASE_URL);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url.toString();
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { ...(options.headers ?? {}) };
  if (browserSession) {
    for (const name of Object.keys(headers)) {
      if (name.toLowerCase() === "authorization") delete headers[name];
    }
    if (options.method && !["GET", "HEAD", "OPTIONS"].includes(options.method)) {
      headers["X-Trackr-Request"] = "browser";
    }
  } else if (options.token) headers.Authorization = `Bearer ${options.token}`;

  let body: BodyInit | undefined;
  if (options.formData) {
    body = options.formData;
  } else if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  }

  const res = await fetch(buildUrl(path, options.query), {
    method: options.method ?? "GET",
    headers,
    body,
    cache: "no-store",
    credentials: browserSession ? "same-origin" : "omit",
  });

  if (!res.ok) {
    let message = res.statusText;
    let isins: string[] | undefined;
    let reasons: string[] | undefined;
    let batchId: string | undefined;
    let isin: string | undefined;
    let deficit: string | undefined;
    let asOf: string | undefined;
    let securityName: string | undefined;
    let securityNames: Record<string, string> | undefined;
    let code: string | undefined;
    let fields: Record<string, unknown> | undefined;
    try {
      const payload = await res.json();
      if (typeof payload?.detail === "string") message = payload.detail;
      else if (Array.isArray(payload?.detail)) {
        // FastAPI's default 422 validation-error shape.
        message = payload.detail.map((d: { msg?: string }) => d.msg).join("; ");
      } else if (payload?.detail && typeof payload.detail === "object") {
        // The Workspace research envelope (BLUEPRINT_BACKEND.md 7.1):
        // {"detail": {"code", "message", "fields", "request_id"}}.
        if (typeof payload.detail.message === "string") message = payload.detail.message;
        if (typeof payload.detail.code === "string") code = payload.detail.code;
        if (payload.detail.fields && typeof payload.detail.fields === "object") {
          fields = payload.detail.fields;
        }
      }
      if (Array.isArray(payload?.isins)) isins = payload.isins;
      if (Array.isArray(payload?.reasons)) reasons = payload.reasons;
      if (typeof payload?.batch_id === "string") batchId = payload.batch_id;
      if (typeof payload?.isin === "string") isin = payload.isin;
      if (typeof payload?.deficit === "string") deficit = payload.deficit;
      if (typeof payload?.as_of === "string") asOf = payload.as_of;
      if (typeof payload?.name === "string") securityName = payload.name;
      if (payload?.names && typeof payload.names === "object") securityNames = payload.names;
      if (typeof payload?.code === "string") code = payload.code;
    } catch {
      // Non-JSON error body -- fall back to statusText.
    }

    // An expired hour is not an error the user should ever see: exchange the dead
    // token for a fresh one and replay the request. The refresh call itself is exempt
    // -- when THAT is refused, the session is truly gone and login is the only way
    // back, so the 401 must surface.
    if (
      res.status === 401 &&
      !options._retry &&
      options.token &&
      unauthorizedHandler &&
      path !== "/auth/refresh" && path !== "/auth/browser/refresh"
    ) {
      const freshToken = await unauthorizedHandler();
      if (freshToken) {
        return request<T>(path, { ...options, token: freshToken, _retry: true });
      }
    }

    throw new ApiError(
      res.status,
      message,
      isins,
      reasons,
      batchId,
      isin,
      deficit,
      asOf,
      securityName,
      securityNames,
      code,
      fields
    );
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// Share only simultaneous reads. No completed portfolio response is retained: every
// navigation/refetch still checks live authorisation and the server's input version.
const pendingGets = new Map<string, Promise<unknown>>();

async function get<T>(path: string, opts?: Omit<RequestOptions, "method">): Promise<T> {
  if (!opts?.token || path.startsWith("/auth/") || opts.json !== undefined || opts.formData) {
    return request<T>(path, { ...opts, method: "GET" });
  }
  const url = new URL(buildUrl(path, opts.query));
  url.searchParams.sort();
  const key = JSON.stringify([
    url.toString(), opts.token, browserSession,
    Object.entries(opts.headers ?? {}).sort(([a], [b]) => a.localeCompare(b)),
  ]);
  const existing = pendingGets.get(key);
  if (existing) return existing as Promise<T>;
  const pending = request<T>(path, { ...opts, method: "GET" }).finally(() => {
    if (pendingGets.get(key) === pending) pendingGets.delete(key);
  });
  pendingGets.set(key, pending);
  return pending;
}

function mutate<T>(path: string, opts: RequestOptions): Promise<T> {
  // A read begun before a write cannot satisfy a refresh after that write finishes.
  pendingGets.clear();
  return request<T>(path, opts).finally(() => pendingGets.clear());
}

export const api = {
  get,
  post: <T>(path: string, opts?: Omit<RequestOptions, "method">) =>
    mutate<T>(path, { ...opts, method: "POST" }),
  put: <T>(path: string, opts?: Omit<RequestOptions, "method">) =>
    mutate<T>(path, { ...opts, method: "PUT" }),
  patch: <T>(path: string, opts?: Omit<RequestOptions, "method">) =>
    mutate<T>(path, { ...opts, method: "PATCH" }),
  delete: <T>(path: string, opts?: Omit<RequestOptions, "method">) =>
    mutate<T>(path, { ...opts, method: "DELETE" }),
};

"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, BROWSER_SESSION, setUnauthorizedHandler } from "./api-client";
import type { LoginRequest, RegisterRequest, TokenResponse, UserResponse } from "./api-types";

const TOKEN_KEY = "trackr.token";
const USER_KEY = "trackr.user";
const INVESTOR_KEY = "trackr.investor_id";
const ADVISED_ONLY_KEY = "trackr.advised_only";

/**
 * Shown INSTEAD of the app when the browser refuses site storage (site data blocked,
 * some embedded or locked-down browsers). The session lives in localStorage, so without
 * it nobody can stay signed in -- say so and stop, rather than crash on load or appear
 * signed out for no stated reason.
 */
const STORAGE_BLOCKED =
  "Your browser is blocking site storage for Trackr+, so it cannot keep you signed in. " +
  "Allow cookies and site data for this site, then reload the page.";

/**
 * Today as NSE reckons it, not as the viewer's clock does.
 *
 * `toISOString()` is UTC, which disagrees with IST for the 5½ hours after midnight in
 * India — so a user opening the app at 2am IST would ask the API for *yesterday*, and the
 * backend (which resolves the trading day in IST, `app/prices/live.py::ist_today`) would
 * treat that as a historical date and refuse to price it live. Every date on screen means
 * a trading day; resolving it in the exchange's timezone is what makes that true.
 */
function today(): string {
  // en-CA gives ISO-shaped YYYY-MM-DD, which is the only reason that locale is here.
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
}

interface AuthState {
  token: string | null;
  user: UserResponse | null;
  investorId: string;
  ready: boolean;
  login: (body: LoginRequest) => Promise<void>;
  register: (body: RegisterRequest) => Promise<void>;
  logout: () => void | Promise<void>;
  setInvestorId: (id: string) => void;
  // Shared header controls -- as_of and advised_only are query params every
  // holdings/summary/performance call accepts (contracts/openapi.yaml), kept here so
  // every page reads/writes the same value instead of duplicating local state.
  asOf: string;
  setAsOf: (date: string) => void;
  advisedOnly: boolean;
  setAdvisedOnly: (value: boolean) => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children, browserSession = false }: {
  children: React.ReactNode; browserSession?: boolean;
}) {
  return browserSession
    ? <BrowserAuthProvider>{children}</BrowserAuthProvider>
    : <LegacyAuthProvider>{children}</LegacyAuthProvider>;
}

function LegacyAuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<UserResponse | null>(null);
  const [investorId, setInvestorIdState] = useState<string>("");
  const [asOf, setAsOfState] = useState<string>(today());
  const [advisedOnly, setAdvisedOnlyState] = useState<boolean>(false);
  const [ready, setReady] = useState(false);
  const [storageError, setStorageError] = useState<string | null>(null);

  useEffect(() => {
    try {
      const storedToken = localStorage.getItem(TOKEN_KEY);
      const storedUser = localStorage.getItem(USER_KEY);
      let parsedUser: UserResponse | null = null;
      if (storedUser) {
        try {
          parsedUser = JSON.parse(storedUser);
        } catch {
          parsedUser = null;
        }
      }
      if (storedToken && !parsedUser) {
        // A token without a readable user record is a broken session, not a half-valid
        // one: end it, so the person signs in again rather than using the app with no
        // identity behind it.
        localStorage.removeItem(TOKEN_KEY);
        localStorage.removeItem(USER_KEY);
        localStorage.removeItem(INVESTOR_KEY);
      } else {
        setToken(storedToken);
        setUser(parsedUser);
        // Written only by applySession from the server's TokenResponse and removed on
        // logout, so it always belongs to the session that left it here. Never a shared
        // default: that is how two accounts once ended up on one portfolio.
        setInvestorIdState(localStorage.getItem(INVESTOR_KEY) ?? "");
      }
      setAdvisedOnlyState(localStorage.getItem(ADVISED_ONLY_KEY) === "true");
    } catch {
      setStorageError(STORAGE_BLOCKED);
    }
    setReady(true);
  }, []);

  /** Run a storage write; a refusal becomes the visible storage error, never a no-op. */
  const persist = useCallback((write: () => void): boolean => {
    try {
      write();
      return true;
    } catch {
      setStorageError(STORAGE_BLOCKED);
      return false;
    }
  }, []);

  const setAsOf = useCallback((date: string) => setAsOfState(date), []);

  const setAdvisedOnly = useCallback(
    (value: boolean) => {
      if (persist(() => localStorage.setItem(ADVISED_ONLY_KEY, String(value)))) {
        setAdvisedOnlyState(value);
      }
    },
    [persist]
  );

  const applySession = useCallback(
    (res: TokenResponse) => {
      const saved = persist(() => {
        localStorage.setItem(TOKEN_KEY, res.token);
        localStorage.setItem(USER_KEY, JSON.stringify(res.user));
        // The server is the only source of "which investor am I". Overwrite whatever a
        // previous session left here -- never merge across accounts.
        if (res.investor_id) {
          localStorage.setItem(INVESTOR_KEY, res.investor_id);
        } else {
          localStorage.removeItem(INVESTOR_KEY);
        }
      });
      if (!saved) return;
      setToken(res.token);
      setUser(res.user);
      setInvestorIdState(res.investor_id ?? "");
    },
    [persist]
  );

  const login = useCallback(
    async (body: LoginRequest) => {
      const res = await api.post<TokenResponse>("/auth/login", { json: body });
      applySession(res);
    },
    [applySession]
  );

  const register = useCallback(
    async (body: RegisterRequest) => {
      const res = await api.post<TokenResponse>("/auth/register", { json: body });
      applySession(res);
    },
    [applySession]
  );

  const logout = useCallback(() => {
    persist(() => {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      // The investor id belongs to the session, not the browser -- leaving it behind is
      // how the next login starts from someone else's portfolio.
      localStorage.removeItem(INVESTOR_KEY);
    });
    setToken(null);
    setUser(null);
    setInvestorIdState("");
  }, [persist]);

  const setInvestorId = useCallback(
    (id: string) => {
      if (persist(() => localStorage.setItem(INVESTOR_KEY, id))) setInvestorIdState(id);
    },
    [persist]
  );

  useEffect(() => {
    // One in-flight refresh shared by every request that hits a dead token at the
    // same moment -- N pages loading at once must not fire N refreshes. The current
    // token is read from localStorage at call time, not from this closure's state,
    // so a second tab that already rotated the session picks up the newer token
    // instead of replaying the revoked one.
    let refreshInFlight: Promise<string | null> | null = null;
    setUnauthorizedHandler(async () => {
      let current: string | null;
      try {
        current = localStorage.getItem(TOKEN_KEY);
      } catch {
        setStorageError(STORAGE_BLOCKED);
        return null;
      }
      if (!current) return null;
      if (!refreshInFlight) {
        const run = async (): Promise<string | null> => {
          try {
            const res = await api.post<TokenResponse>("/auth/refresh", { token: current });
            applySession(res);
            return res.token;
          } catch {
            logout();
            return null;
          }
        };
        refreshInFlight = run().finally(() => {
          refreshInFlight = null;
        });
      }
      return refreshInFlight;
    });
    return () => setUnauthorizedHandler(null);
  }, [applySession, logout]);

  const value = useMemo(
    () => ({
      token,
      user,
      investorId,
      ready,
      login,
      register,
      logout,
      setInvestorId,
      asOf,
      setAsOf,
      advisedOnly,
      setAdvisedOnly,
    }),
    [
      token,
      user,
      investorId,
      ready,
      login,
      register,
      logout,
      setInvestorId,
      asOf,
      setAsOf,
      advisedOnly,
      setAdvisedOnly,
    ]
  );

  if (storageError) {
    return (
      <div
        role="alert"
        style={{
          maxWidth: 520,
          margin: "15vh auto 0",
          padding: "20px 24px",
          borderRadius: "var(--radius-md)",
          background: "var(--negative-tint)",
          color: "var(--negative)",
          font: "var(--type-body)",
        }}
      >
        {storageError}
      </div>
    );
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}


type BrowserIdentity = { user: UserResponse; investor_id: string | null };

function BrowserAuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<UserResponse | null>(null);
  const [investorId, setInvestorIdState] = useState("");
  const [ready, setReady] = useState(false);
  const [asOf, setAsOfState] = useState(today);
  const [advisedOnly, setAdvisedOnlyState] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const refreshInFlight = useRef<Promise<string | null> | null>(null);

  const applyIdentity = useCallback((identity: BrowserIdentity | null) => {
    setUser(identity?.user ?? null);
    setInvestorIdState(identity?.investor_id ?? "");
    if (identity) document.documentElement.dataset.session = "1";
    else delete document.documentElement.dataset.session;
  }, []);

  const refresh = useCallback((): Promise<string | null> => {
    if (!refreshInFlight.current) {
      const mine = generation.current;
      refreshInFlight.current = api.post<BrowserIdentity>("/auth/browser/refresh")
        .then((identity) => {
          if (mine !== generation.current) return null;
          applyIdentity(identity);
          return BROWSER_SESSION;
        })
        .catch((err: unknown) => {
          if (mine === generation.current && err instanceof ApiError && err.status === 401) {
            applyIdentity(null);
          }
          return null;
        })
        .finally(() => { refreshInFlight.current = null; });
    }
    return refreshInFlight.current;
  }, [applyIdentity]);

  useEffect(() => {
    let cancelled = false;
    const mine = generation.current;
    try {
      // Retire credentials from older Plus versions; never read or transmit them.
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      localStorage.removeItem(INVESTOR_KEY);
      setAdvisedOnlyState(localStorage.getItem(ADVISED_ONLY_KEY) === "true");
    } catch { /* Optional display preferences never gate cookie sign-in. */ }
    setUnauthorizedHandler(refresh);
    api.get<BrowserIdentity>("/auth/browser/session")
      .then((identity) => {
        if (!cancelled && mine === generation.current) applyIdentity(identity);
      })
      .catch(async (err: unknown) => {
        if (cancelled || mine !== generation.current) return;
        if (err instanceof ApiError && err.status === 401) await refresh();
        else setError("Could not check your session. Please reload and try again.");
      })
      .finally(() => { if (!cancelled) setReady(true); });
    return () => { cancelled = true; setUnauthorizedHandler(null); };
  }, [applyIdentity, refresh]);

  const login = useCallback(async (body: LoginRequest) => {
    generation.current += 1;
    applyIdentity(await api.post<BrowserIdentity>("/auth/browser/login", { json: body }));
    setError(null);
  }, [applyIdentity]);
  const register = useCallback(async (body: RegisterRequest) => {
    generation.current += 1;
    applyIdentity(await api.post<BrowserIdentity>("/auth/browser/register", { json: body }));
    setError(null);
  }, [applyIdentity]);
  const logout = useCallback(async () => {
    generation.current += 1;
    // Revoke and clear the server cookie before reporting that sign-out succeeded.
    if (refreshInFlight.current) await refreshInFlight.current;
    try {
      await api.post("/auth/browser/logout");
      applyIdentity(null);
      setError(null);
    } catch (err) {
      setError("Sign-out failed. Please try signing out again.");
      throw err;
    }
  }, [applyIdentity]);
  const setAdvisedOnly = useCallback((value: boolean) => {
    setAdvisedOnlyState(value);
    try { localStorage.setItem(ADVISED_ONLY_KEY, String(value)); } catch { /* Optional. */ }
  }, []);
  const value = useMemo(() => ({
    // Compatibility flag for existing Plus screens. This is not an access credential.
    token: user ? `${BROWSER_SESSION}:${user.id}:${generation.current}` : null,
    user, investorId, ready, login, register, logout,
    setInvestorId: setInvestorIdState, asOf, setAsOf: setAsOfState, advisedOnly, setAdvisedOnly,
  }), [user, investorId, ready, login, register, logout, asOf, advisedOnly, setAdvisedOnly]);
  return <AuthContext.Provider value={value}>
    {error && <div role="alert">{error}</div>}
    {children}
  </AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}

export { ApiError };

import { FormEvent, ReactNode, createContext, useContext, useEffect, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { request, setUnauthorizedHandler } from "./api";
import { errorMessage } from "./copy";
import { openEventStream, stopEventStream } from "./useEventStream";

export type AuthUser = {
  id: string;
  email: string;
  is_admin: boolean;
};

type AuthValue = {
  user: AuthUser | null;
  ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthValue | null>(null);

function safeNext(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return "/";
  return value;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let stop = false;
    request<AuthUser>("/api/auth/me")
      .then((me) => {
        if (!stop) setUser(me);
      })
      .catch(() => {
        if (!stop) setUser(null);
      })
      .finally(() => {
        if (!stop) setReady(true);
      });
    return () => {
      stop = true;
    };
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null);
      stopEventStream();
      const path = window.location.pathname;
      if (path === "/login" || path === "/register") return;
      const next = `${path}${window.location.search}`;
      navigate(`/login?next=${encodeURIComponent(next)}`, { replace: true });
    });
  }, [navigate]);

  useEffect(() => {
    if (!user) return;
    const session = openEventStream({
      fetchState: () => undefined,
      onMessage: () => undefined,
    });
    return () => {
      session.close();
      stopEventStream();
    };
  }, [user]);

  async function login(email: string, password: string) {
    const me = await request<AuthUser>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    setUser(me);
  }

  async function register(email: string, password: string) {
    await request("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const me = await request<AuthUser>("/api/auth/me");
    setUser(me);
  }

  async function logout() {
    await request("/api/auth/logout", { method: "POST" });
    setUser(null);
    stopEventStream();
    navigate("/login", { replace: true });
  }

  return <AuthContext.Provider value={{ user, ready, login, register, logout }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used within AuthProvider");
  return value;
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, ready } = useAuth();
  const location = useLocation();
  if (!ready) return null;
  if (!user) {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }
  return children;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block text-xs uppercase tracking-[0.18em] text-stark-gray">
      {label}
      <div className="mt-3 normal-case tracking-normal">{children}</div>
    </label>
  );
}

function failure(err: unknown): string {
  if (err && typeof err === "object" && "code" in err) {
    const code = (err as { code?: string }).code;
    const message = err instanceof Error ? err.message : null;
    return errorMessage(code, message);
  }
  if (err instanceof Error) return err.message;
  return "The request did not complete.";
}

export function LoginPage() {
  const { user, ready, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const next = safeNext(new URLSearchParams(location.search).get("next"));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [allowSignup, setAllowSignup] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let stop = false;
    request<{ allow_signup?: boolean }>("/api/meta/config")
      .then((config) => {
        if (!stop) setAllowSignup(Boolean(config.allow_signup));
      })
      .catch(() => {
        if (!stop) setAllowSignup(false);
      });
    return () => {
      stop = true;
    };
  }, []);

  useEffect(() => {
    if (ready && user) navigate(next, { replace: true });
  }, [ready, user, next, navigate]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      navigate(next, { replace: true });
    } catch (err) {
      setError(failure(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="mx-auto mt-16 max-w-md border border-white/15 bg-stark-ink p-6">
      <p className="text-xs uppercase tracking-[0.28em] text-stark-gold">Sign in</p>
      <h1 className="mt-3 text-3xl font-semibold">Welcome back</h1>
      <div className="mt-6 space-y-4">
        <Field label="Email">
          <input value={email} onChange={(event) => setEmail(event.target.value)} type="email" autoComplete="username" required className="w-full bg-black p-2 text-sm" />
        </Field>
        <Field label="Password">
          <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete="current-password" required className="w-full bg-black p-2 text-sm" />
        </Field>
      </div>
      {error && <p className="mt-4 text-sm text-stark-gold" role="alert">{error}</p>}
      <button disabled={busy} className="mt-6 bg-stark-gold px-5 py-2 text-sm font-semibold uppercase tracking-[0.16em] text-black disabled:opacity-50">
        {busy ? "Signing in" : "Sign in"}
      </button>
      {allowSignup && (
        <p className="mt-4 text-sm text-stark-gray">
          <a className="text-stark-gold" href="/register">Create an account</a>
        </p>
      )}
    </form>
  );
}

export function RegisterPage() {
  const { user, ready, register } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [allowSignup, setAllowSignup] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let stop = false;
    request<{ allow_signup?: boolean }>("/api/meta/config")
      .then((config) => {
        if (!stop) setAllowSignup(Boolean(config.allow_signup));
      })
      .catch(() => {
        if (!stop) setAllowSignup(false);
      });
    return () => {
      stop = true;
    };
  }, []);

  useEffect(() => {
    if (ready && user) navigate("/", { replace: true });
  }, [ready, user, navigate]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await register(email, password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(failure(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="mx-auto mt-16 max-w-md border border-white/15 bg-stark-ink p-6">
      <p className="text-xs uppercase tracking-[0.28em] text-stark-gold">Create account</p>
      <h1 className="mt-3 text-3xl font-semibold">Start translating</h1>
      {allowSignup === false && <p className="mt-6 text-sm text-stark-gold">{errorMessage("signup_disabled")}</p>}
      {allowSignup && (
        <>
          <div className="mt-6 space-y-4">
            <Field label="Email">
              <input value={email} onChange={(event) => setEmail(event.target.value)} type="email" autoComplete="username" required className="w-full bg-black p-2 text-sm" />
            </Field>
            <Field label="Password">
              <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete="new-password" required minLength={10} className="w-full bg-black p-2 text-sm" />
            </Field>
          </div>
          {error && <p className="mt-4 text-sm text-stark-gold" role="alert">{error}</p>}
          <button disabled={busy} className="mt-6 bg-stark-gold px-5 py-2 text-sm font-semibold uppercase tracking-[0.16em] text-black disabled:opacity-50">
            {busy ? "Creating" : "Create account"}
          </button>
        </>
      )}
      <p className="mt-4 text-sm text-stark-gray">
        <a className="text-stark-gold" href="/login">Sign in</a>
      </p>
    </form>
  );
}

type TokenRecord = {
  id: string;
  name: string;
  prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
};

export function TokensPage() {
  const [tokens, setTokens] = useState<TokenRecord[]>([]);
  const [name, setName] = useState("");
  const [fresh, setFresh] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setTokens(await request<TokenRecord[]>("/api/auth/tokens"));
  }

  useEffect(() => {
    load().catch((err) => setError(failure(err)));
  }, []);

  async function create(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setCopied(false);
    try {
      const created = await request<TokenRecord & { token: string }>("/api/auth/tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      setFresh(created.token);
      setName("");
      await load();
    } catch (err) {
      setError(failure(err));
    }
  }

  async function revoke(id: string) {
    setError(null);
    try {
      await request(`/api/auth/tokens/${id}`, { method: "DELETE" });
      await load();
    } catch (err) {
      setError(failure(err));
    }
  }

  async function copy() {
    if (!fresh) return;
    await navigator.clipboard.writeText(fresh);
    setCopied(true);
  }

  return (
    <section className="mx-auto max-w-2xl">
      <p className="text-xs uppercase tracking-[0.28em] text-stark-gold">Settings</p>
      <h1 className="mt-3 text-3xl font-semibold">Tokens</h1>
      <p className="mt-3 text-sm text-stark-gray">A token lets an MCP client translate as you. The secret is shown once.</p>
      <form onSubmit={create} className="mt-8 flex gap-3 border border-white/15 bg-stark-ink p-4">
        <input value={name} onChange={(event) => setName(event.target.value)} required placeholder="Name" className="w-full bg-black p-2 text-sm" />
        <button className="shrink-0 bg-stark-gold px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-black">Create</button>
      </form>
      {fresh && (
        <div className="mt-4 border border-stark-gold/60 p-4">
          <p className="text-xs uppercase tracking-[0.18em] text-stark-gold">Copy this token now</p>
          <input readOnly value={fresh} className="mt-3 w-full bg-black p-2 font-mono text-sm" />
          <button type="button" onClick={() => void copy()} className="mt-3 text-xs uppercase tracking-[0.16em] text-stark-gold">
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      )}
      {error && <p className="mt-4 text-sm text-stark-gold" role="alert">{error}</p>}
      <ul className="mt-8 divide-y divide-white/10 border border-white/10">
        {tokens.length === 0 && <li className="px-4 py-6 text-sm text-stark-gray">No tokens yet.</li>}
        {tokens.map((token) => (
          <li key={token.id} className="flex items-center justify-between gap-4 px-4 py-3 text-sm">
            <div>
              <div>{token.name}</div>
              <p className="mt-1 font-mono text-xs text-stark-gray">stk_{token.prefix}…{token.revoked_at ? " · revoked" : ""}</p>
            </div>
            {!token.revoked_at && (
              <button type="button" onClick={() => void revoke(token.id)} className="text-xs uppercase tracking-[0.16em] text-stark-gold">Revoke</button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

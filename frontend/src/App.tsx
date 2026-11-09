import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { ApiError, DocumentRecord, EstimateModel, JobRecord, SegmentRecord, request } from "./api";
import { AuthProvider, LoginPage, RegisterPage, RequireAuth, TokensPage, useAuth } from "./auth";
import { errorMessage, progressLabel, sameNotice } from "./copy";
import { applySnapshot, isTerminal, mergeState, useEventStream } from "./useEventStream";

const rememberedJobs = new Map<string, JobRecord>();
const DETAIL_STATUSES = new Set(["translating", "reviewing", "rendering", "done", "failed"]);
const RETRYABLE = new Set(["rate_limited", "funds_exceeded", "provider_timeout", "provider_error"]);

type CostReport = {
  stages: { stage: string; cost_cents: number; tokens_in: number; tokens_out: number }[];
};

function adoptJobs(prev: Map<string, JobRecord>, rows: JobRecord[]): Map<string, JobRecord> {
  const next = new Map<string, JobRecord>();
  for (const row of rows) {
    next.set(row.id, applySnapshot(prev.get(row.id) ?? rememberedJobs.get(row.id), row, "job"));
  }
  rememberedJobs.clear();
  for (const job of next.values()) rememberedJobs.set(job.id, job);
  return next;
}

function rememberJob(job: JobRecord) {
  rememberedJobs.set(job.id, job);
}

function formatStamp(value: string | null | undefined): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleString();
}

function signedPercent(spent: number, estimate: number): string {
  const pct = Math.round(((spent - estimate) / estimate) * 100);
  if (pct > 0) return `+${pct}%`;
  return `${pct}%`;
}

function readFailure(err: unknown): { code?: string; detail?: string } {
  if (err instanceof ApiError) return { code: err.code, detail: err.message };
  if (err instanceof Error) return { detail: err.message };
  return { detail: "The request did not complete." };
}

function Shell({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  return (
    <div className="min-h-screen bg-stark-black text-stark-white">
      <header className="border-b border-white/10">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-5">
          <Link to="/" className="flex items-center gap-3">
            <span className="grid h-9 w-9 place-items-center rounded-full border border-stark-gold text-xs font-semibold tracking-widest text-stark-gold">SF</span>
            <span className="text-sm font-semibold tracking-[0.35em]">STARK</span>
          </Link>
          {user && (
            <nav className="flex items-center gap-4 text-xs uppercase tracking-[0.2em] text-stark-gray">
              <span className="normal-case tracking-normal text-stark-white">{user.email}</span>
              <Link to="/settings/tokens">Tokens</Link>
              <button type="button" onClick={() => void logout()} className="uppercase tracking-[0.2em]">Log out</button>
            </nav>
          )}
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-10">{children}</main>
    </div>
  );
}

function ErrorPanel({ code, detail }: { code?: string | null; detail?: string | null }) {
  if (!code && !detail) return null;
  const message = errorMessage(code, detail);
  const extra = detail && !sameNotice(message, detail) ? detail : null;
  return (
    <div className="border border-stark-gold/60 bg-stark-ink px-4 py-3 text-sm text-stark-gold" role="alert">
      <div className="text-xs uppercase tracking-[0.18em]">Needs attention</div>
      <p className="mt-1 text-stark-white">{message}</p>
      {extra && <p className="mt-1 text-stark-gray">{extra}</p>}
      {code && <p className="mt-1 font-mono text-xs text-stark-gray">{code}</p>}
    </div>
  );
}

function Home() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<Map<string, JobRecord>>(() => new Map(rememberedJobs));
  const [maxUploadMb, setMaxUploadMb] = useState<number | null>(null);
  const [error, setError] = useState<{ code?: string; detail?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const jobsRef = useRef(jobs);
  jobsRef.current = jobs;
  const unknownFlight = useRef<Promise<void> | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    let stop = false;
    request<{ max_upload_mb: number }>("/api/meta/config")
      .then((config) => {
        if (!stop) setMaxUploadMb(config.max_upload_mb);
      })
      .catch(() => {
        if (!stop) setMaxUploadMb(null);
      });
    return () => {
      stop = true;
    };
  }, []);

  useEffect(() => {
    let stop = false;
    request<JobRecord[]>("/api/jobs")
      .then((rows) => {
        if (!stop) setJobs((prev) => adoptJobs(prev, rows));
      })
      .catch(() => undefined);
    return () => {
      stop = true;
    };
  }, []);

  function refreshUnknown() {
    if (unknownFlight.current) return;
    const run = request<JobRecord[]>("/api/jobs")
      .then((rows) => {
        if (!mounted.current) return;
        setJobs((prev) => adoptJobs(prev, rows));
      })
      .catch(() => undefined)
      .finally(() => {
        unknownFlight.current = null;
      });
    unknownFlight.current = run;
  }

  useEventStream({
    fetchState: async () => {
      const rows = await request<JobRecord[]>("/api/jobs");
      if (!mounted.current) return;
      setJobs((prev) => adoptJobs(prev, rows));
    },
    onMessage: (msg) => {
      if (msg.type !== "job") return;
      const existing = jobsRef.current.get(msg.id);
      if (!existing) {
        refreshUnknown();
        return;
      }
      setJobs((prev) => {
        const current = prev.get(msg.id);
        if (!current) return prev;
        const next = new Map(prev);
        const merged = mergeState(current, msg);
        next.set(msg.id, merged);
        rememberedJobs.set(msg.id, merged);
        jobsRef.current = next;
        return next;
      });
    },
  });

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = event.currentTarget.elements.namedItem("file") as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    if (maxUploadMb != null && file.size > maxUploadMb * 1024 * 1024) {
      setError({ code: "file_too_large", detail: `Limit is ${maxUploadMb} MB.` });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const body = new FormData();
      body.append("file", file);
      const document = await request<DocumentRecord>("/api/documents", { method: "POST", body });
      navigate(`/documents/${document.id}`);
    } catch (err) {
      setError(readFailure(err));
    } finally {
      setBusy(false);
    }
  }

  const history = [...jobs.values()];
  return (
    <Shell>
      <p className="text-xs uppercase tracking-[0.28em] text-stark-gold">Document translator</p>
      <h1 className="mt-3 max-w-3xl text-4xl font-semibold tracking-tight">Translate the file. Keep the structure.</h1>
      <p className="mt-4 max-w-2xl text-stark-gray">PDF, DOCX, or a photo of a page. You see the price before anything is sent to the model.</p>
      <form onSubmit={onSubmit} className="mt-8 border border-white/15 bg-stark-ink p-6">
        <label className="block text-xs uppercase tracking-[0.18em] text-stark-gray">
          File
          <input name="file" type="file" accept=".pdf,.docx,.png,.jpg,.jpeg" className="mt-3 block w-full text-sm" required />
        </label>
        <button disabled={busy} className="mt-6 bg-stark-gold px-5 py-2 text-sm font-semibold uppercase tracking-[0.16em] text-black disabled:opacity-50">
          {busy ? "Uploading" : "Analyze"}
        </button>
        {error && <div className="mt-4"><ErrorPanel code={error.code} detail={error.detail} /></div>}
      </form>
      <h2 className="mt-12 text-xs uppercase tracking-[0.22em] text-stark-gray">History</h2>
      <ul className="mt-4 divide-y divide-white/10 border border-white/10">
        {history.length === 0 && <li className="px-4 py-6 text-sm text-stark-gray">No translations yet.</li>}
        {history.map((job) => {
          const stamp = formatStamp(job.created_at);
          const title = job.document_filename || "Untitled document";
          return (
            <li key={job.id} className="flex items-center justify-between gap-4 px-4 py-3 text-sm">
              <div className="min-w-0">
                <Link to={`/jobs/${job.id}`} title={title} className="block truncate hover:text-stark-gold">{title}</Link>
                <p className="mt-1 truncate text-stark-gray">
                  {job.target_lang.toUpperCase()} · {job.model}{stamp ? ` · ${stamp}` : ""}
                </p>
              </div>
              <span className="shrink-0 text-stark-gray">{job.status}</span>
            </li>
          );
        })}
      </ul>
    </Shell>
  );
}

function DocumentPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [document, setDocument] = useState<DocumentRecord | null>(null);
  const [model, setModel] = useState("gpt-4o-mini");
  const [target, setTarget] = useState("de");
  const [mode, setMode] = useState("layout");
  const [languages, setLanguages] = useState<{ code: string; name: string }[]>([]);
  const [quotes, setQuotes] = useState<EstimateModel[] | null>(null);
  const [error, setError] = useState<{ code?: string; detail?: string } | null>(null);
  const idRef = useRef(id);
  idRef.current = id;

  useEffect(() => {
    request<{ code: string; name: string }[]>("/api/meta/languages").then(setLanguages).catch(() => setLanguages([]));
  }, []);

  useEffect(() => {
    setDocument(null);
    setQuotes(null);
    setError(null);
  }, [id]);

  useEffect(() => {
    const documentId = id;
    if (!documentId || document?.analysis_status !== "ready") return;
    let cancelled = false;
    const timer = setTimeout(() => {
      request<{ models: EstimateModel[] }>(`/api/documents/${documentId}/estimate?target_lang=${encodeURIComponent(target)}`)
        .then((body) => {
          if (!cancelled && idRef.current === documentId) setQuotes(body.models);
        })
        .catch((err) => {
          if (!cancelled && idRef.current === documentId) setError(readFailure(err));
        });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [id, target, document?.analysis_status]);

  async function refreshDocument() {
    const documentId = idRef.current;
    if (!documentId) return;
    try {
      const next = await request<DocumentRecord>(`/api/documents/${documentId}`);
      if (idRef.current !== documentId) return;
      setDocument((current) => applySnapshot(current ?? undefined, next, "document"));
      setError(null);
    } catch (err) {
      if (idRef.current !== documentId) return;
      setError(readFailure(err));
    }
  }

  useEventStream({
    fetchState: refreshDocument,
    onMessage: (msg) => {
      if (msg.type !== "document" || msg.id !== idRef.current) return;
      setDocument((current) => (current ? mergeState(current, msg) : current));
      if (isTerminal("document", msg.analysis_status)) void refreshDocument();
    },
  });

  async function translate() {
    setError(null);
    try {
      const job = await request<JobRecord>("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ document_id: id, target_lang: target, model, render_mode: mode }),
      });
      rememberJob(job);
      navigate(`/jobs/${job.id}`);
    } catch (err) {
      setError(readFailure(err));
    }
  }

  const lines = document?.analysis?.class_lines || [];
  const poor = (document?.analysis?.pages || []).filter((page) => page.class === "scanned_poor" && page.thumbnail);
  return (
    <Shell>
      <p className="text-xs uppercase tracking-[0.22em] text-stark-gray">{document?.filename || "Analyzing"}</p>
      <h1 className="mt-2 text-3xl font-semibold">Price before you translate</h1>
      {document?.analysis_status !== "ready" && document?.analysis_status !== "failed" && (
        <p className="mt-6 text-stark-gray">Reading pages and classifying each one.</p>
      )}
      {document?.analysis_status === "failed" && <div className="mt-6"><ErrorPanel code={document.error_code} detail={document.error_detail} /></div>}
      {error && <div className="mt-6"><ErrorPanel code={error.code} detail={error.detail} /></div>}
      {document?.analysis_status === "ready" && document.analysis && (
        <div className="mt-8 grid gap-6 lg:grid-cols-[1.2fr_0.8fr]">
          <section className="border border-white/10 p-5">
            <p className="text-xs uppercase tracking-[0.18em] text-stark-gray">{document.page_count} pages · {document.char_count} characters · detected {document.detected_source_lang || "unknown"}</p>
            <ul className="mt-4 space-y-2 text-sm">
              {lines.map((line) => (
                <li key={line.class} className="border-b border-white/10 py-2">
                  <div className="flex justify-between">
                    <span>{line.class.replaceAll("_", " ")}</span>
                    <span className="text-stark-gold">{line.pages}</span>
                  </div>
                  <p className="mt-1 text-xs text-stark-gray">
                    {line.action === "translate" && "Included in the model price."}
                    {line.action === "ocr" && `OCR $${(line.cost_cents / 100).toFixed(4)}`}
                    {line.action === "ocr_enhance" && `OCR after enhancement $${(line.cost_cents / 100).toFixed(4)}`}
                    {line.action === "skipped" && "Skipped. No charge."}
                    {line.action === "rejected" && "Rejected before reading."}
                    {line.warning ? ` ${line.warning}` : ""}
                  </p>
                </li>
              ))}
            </ul>
            {poor.length > 0 && (
              <div className="mt-4 flex gap-3 overflow-x-auto">
                {poor.map((page) => (
                  <figure key={page.page} className="shrink-0">
                    <img className="h-28 border border-white/10" src={`/api/documents/${id}/pages/${page.page}/thumbnail`} alt={`Page ${page.page}`} />
                    <figcaption className="mt-1 text-xs text-stark-gray">Page {page.page}</figcaption>
                  </figure>
                ))}
              </div>
            )}
            {document.analysis.ocr_pages.length > 0 && (
              <p className="mt-4 text-sm text-stark-gray">Scanned pages {document.analysis.ocr_pages.join(", ")} will be read before translation. Poor-quality pages are enhanced and may be less accurate.</p>
            )}
          </section>
          <section className="border border-white/10 p-5">
            <label className="block text-xs uppercase tracking-[0.16em] text-stark-gray">Target language
              <select value={target} onChange={(event) => setTarget(event.target.value)} className="mt-2 w-full bg-black p-2 text-sm">
                {languages.map((language) => <option key={language.code} value={language.code}>{language.name}</option>)}
              </select>
            </label>
            <div className="mt-4 space-y-2">
              {(quotes ?? document.analysis.models).map((item) => (
                <label key={item.model} className={`block border p-3 text-sm ${model === item.model ? "border-stark-gold" : "border-white/10"}`}>
                  <input type="radio" name="model" className="mr-2" checked={model === item.model} onChange={() => setModel(item.model)} />
                  {item.model} · {item.tier}
                  <div className="mt-1 text-stark-gray">{item.segments} segments · ~${(item.cost_cents / 100).toFixed(4)} · OCR ${(item.ocr_cost_cents / 100).toFixed(4)} · ~{item.duration_seconds}s</div>
                </label>
              ))}
            </div>
            <label className="mt-4 block text-xs uppercase tracking-[0.16em] text-stark-gray">PDF layout
              <select value={mode} onChange={(event) => setMode(event.target.value)} className="mt-2 w-full bg-black p-2 text-sm normal-case tracking-normal">
                <option value="layout">Preserve layout</option>
                <option value="reflow">Reflow</option>
              </select>
            </label>
            <button onClick={translate} className="mt-5 bg-stark-gold px-5 py-2 text-sm font-semibold uppercase tracking-[0.16em] text-black">Translate</button>
          </section>
        </div>
      )}
    </Shell>
  );
}

function JobPage() {
  const { id } = useParams();
  const [job, setJob] = useState<JobRecord | null>(null);
  const [segments, setSegments] = useState<SegmentRecord[]>([]);
  const [cost, setCost] = useState<CostReport | null>(null);
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [retrying, setRetrying] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const idRef = useRef(id);
  idRef.current = id;
  const jobRef = useRef(job);
  jobRef.current = job;
  const detailTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setJob(null);
    setSegments([]);
    setCost(null);
    setDrafts({});
    setRetrying(false);
    setActionError(null);
  }, [id]);

  useEffect(() => {
    return () => {
      if (detailTimer.current) clearTimeout(detailTimer.current);
    };
  }, [id]);

  async function loadDetails(jobId: string, status: string) {
    if (!DETAIL_STATUSES.has(status)) return;
    const [nextSegments, nextCost] = await Promise.all([
      request<SegmentRecord[]>(`/api/jobs/${jobId}/segments`),
      request<CostReport>(`/api/jobs/${jobId}/cost`),
    ]);
    if (idRef.current !== jobId) return;
    setSegments(nextSegments);
    setCost(nextCost);
  }

  async function refreshJob() {
    const jobId = idRef.current;
    if (!jobId) return;
    const next = await request<JobRecord>(`/api/jobs/${jobId}`);
    if (idRef.current !== jobId) return;
    setJob((current) => applySnapshot(current ?? undefined, next, "job"));
    await loadDetails(jobId, next.status);
  }

  function scheduleDetails() {
    if (detailTimer.current) clearTimeout(detailTimer.current);
    detailTimer.current = setTimeout(() => {
      detailTimer.current = null;
      const current = jobRef.current;
      const jobId = idRef.current;
      if (!current || !jobId) return;
      void loadDetails(jobId, current.status);
    }, 1000);
  }

  useEventStream({
    fetchState: refreshJob,
    onMessage: (msg) => {
      if (msg.type !== "job" || msg.id !== idRef.current) return;
      setJob((current) => (current ? mergeState(current, msg) : current));
      if (isTerminal("job", msg.status)) {
        if (detailTimer.current) {
          clearTimeout(detailTimer.current);
          detailTimer.current = null;
        }
        void refreshJob();
        return;
      }
      scheduleDetails();
    },
  });

  async function retry() {
    const jobId = idRef.current;
    if (!jobId) return;
    setRetrying(true);
    setActionError(null);
    try {
      const next = await request<JobRecord>(`/api/jobs/${jobId}/retry`, { method: "POST" });
      const resumed = { ...next, seq: Date.now() };
      setJob(resumed);
      rememberJob(resumed);
      await loadDetails(jobId, resumed.status);
    } catch (err) {
      const failure = readFailure(err);
      setActionError(errorMessage(failure.code, failure.detail));
    } finally {
      setRetrying(false);
    }
  }

  async function save(blockIdx: number) {
    await request(`/api/jobs/${id}/segments/0`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ block_idx: blockIdx, text: drafts[blockIdx] }),
    });
    setSegments(await request<SegmentRecord[]>(`/api/jobs/${id}/segments`));
  }

  const ratio = job && job.total_segments ? Math.round((job.done_segments / job.total_segments) * 100) : job?.status === "done" ? 100 : 8;
  return (
    <Shell>
      {job?.document_filename && (
        <h1 className="truncate text-2xl font-semibold" title={job.document_filename}>{job.document_filename}</h1>
      )}
      <p className={`${job?.document_filename ? "mt-2" : ""} text-xs uppercase tracking-[0.22em] text-stark-gold`}>
        {job ? progressLabel(job.done_segments, job.total_segments, job.status) : "Loading"}
      </p>
      <div className="mt-4 h-1 bg-white/10">
        <div className="h-1 bg-stark-gold" style={{ width: `${Math.min(ratio, 100)}%` }} />
      </div>
      {job && (
        <p className="mt-3 text-sm text-stark-gray">
          {job.estimate_cents != null && <>Estimated ${(job.estimate_cents / 100).toFixed(4)} · </>}
          Spent ${(job.cost_cents / 100).toFixed(4)}
          {job.status === "done" && job.estimate_cents ? ` · ${signedPercent(job.cost_cents, job.estimate_cents)}` : ""}
          {` · ${job.model} · ${job.target_lang}`}
        </p>
      )}
      {job?.quality_warning && <p className="mt-3 text-sm text-stark-gold">A reviewer retranslated at least one segment because the first pass failed a quality check.</p>}
      {job?.error_code && <div className="mt-4"><ErrorPanel code={job.error_code} detail={job.error_detail} /></div>}
      {job?.status === "failed" && job.error_code && RETRYABLE.has(job.error_code) && (
        <button type="button" disabled={retrying} onClick={() => void retry()} className="mt-4 bg-stark-gold px-5 py-2 text-sm font-semibold uppercase tracking-[0.16em] text-black disabled:opacity-50">
          {retrying ? "Retrying" : "Retry"}
        </button>
      )}
      {actionError && <p className="mt-3 text-sm text-stark-gold" role="alert">{actionError}</p>}
      {cost && (
        <ul className="mt-6 grid gap-2 sm:grid-cols-3">
          {cost.stages.map((stage) => (
            <li key={stage.stage} className="border border-white/10 p-3 text-sm">
              <div className="text-xs uppercase tracking-[0.16em] text-stark-gray">{stage.stage}</div>
              <div className="mt-1">{stage.tokens_in + stage.tokens_out} tokens · ${(stage.cost_cents / 100).toFixed(4)}</div>
            </li>
          ))}
        </ul>
      )}
      {job?.status === "done" && (
        <a className="mt-6 inline-block border border-stark-gold px-4 py-2 text-sm uppercase tracking-[0.16em] text-stark-gold" href={`/api/jobs/${id}/download`}>Download</a>
      )}
      <div className="mt-8 space-y-4">
        {segments.map((segment) => (
          <article key={segment.idx} className="border border-white/10">
            <header className="flex justify-between px-4 py-2 text-xs uppercase tracking-[0.16em] text-stark-gray">
              <span>Segment {segment.idx}</span>
              <span>{segment.status}{segment.review_state ? ` · ${segment.review_state}` : ""}</span>
            </header>
            {segment.blocks.filter((block) => block.kind !== "image").map((block) => (
              <div key={block.block_idx} className="grid gap-0 border-t border-white/10 md:grid-cols-2">
                <p className="whitespace-pre-wrap p-4 text-sm text-stark-gray">{block.source}</p>
                <div className="border-t border-white/10 p-4 md:border-l md:border-t-0">
                  <textarea
                    className="h-28 w-full bg-transparent text-sm outline-none"
                    value={drafts[block.block_idx] ?? block.target ?? ""}
                    onChange={(event) => setDrafts({ ...drafts, [block.block_idx]: event.target.value })}
                  />
                  <button className="text-xs uppercase tracking-[0.16em] text-stark-gold" onClick={() => save(block.block_idx)}>Save edit</button>
                </div>
              </div>
            ))}
          </article>
        ))}
      </div>
    </Shell>
  );
}

const routes: { path: string; element: ReactNode }[] = [
  { path: "/login", element: <Shell><LoginPage /></Shell> },
  { path: "/register", element: <Shell><RegisterPage /></Shell> },
  { path: "/", element: <RequireAuth><Home /></RequireAuth> },
  { path: "/documents/:id", element: <RequireAuth><DocumentPage /></RequireAuth> },
  { path: "/jobs/:id", element: <RequireAuth><JobPage /></RequireAuth> },
  { path: "/settings/tokens", element: <RequireAuth><Shell><TokensPage /></Shell></RequireAuth> },
];

export function App() {
  return (
    <AuthProvider>
      <Routes>
        {routes.map((route) => (
          <Route key={route.path} path={route.path} element={route.element} />
        ))}
      </Routes>
    </AuthProvider>
  );
}

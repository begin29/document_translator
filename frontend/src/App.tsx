import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { ApiError, DocumentRecord, EstimateModel, JobMessage, JobRecord, SegmentRecord, request } from "./api";
import { AuthProvider, LoginPage, RegisterPage, RequireAuth, TokensPage, useAuth } from "./auth";
import { errorMessage, progressLabel, progressRatio, sameNotice } from "./copy";
import { applySnapshot, isTerminal, mergeJobRecord, mergeState, useEventStream } from "./useEventStream";

const rememberedJobs = new Map<string, JobRecord>();
const RETRYABLE = new Set(["rate_limited", "funds_exceeded", "provider_timeout", "provider_error"]);

type CostReport = {
  stages: { stage: string; cost_cents: number; tokens_in: number; tokens_out: number }[];
};

const HISTORY_PAGE = 10;

function snapshotJob(prev: Map<string, JobRecord>, row: JobRecord): JobRecord {
  return applySnapshot(prev.get(row.id) ?? rememberedJobs.get(row.id), row, "job");
}

function storeJobs(next: Map<string, JobRecord>): Map<string, JobRecord> {
  rememberedJobs.clear();
  for (const job of next.values()) rememberedJobs.set(job.id, job);
  return next;
}

function adoptJobs(prev: Map<string, JobRecord>, rows: JobRecord[]): Map<string, JobRecord> {
  const next = new Map<string, JobRecord>();
  for (const row of rows) next.set(row.id, snapshotJob(prev, row));
  return storeJobs(next);
}

function appendJobs(prev: Map<string, JobRecord>, rows: JobRecord[]): Map<string, JobRecord> {
  const next = new Map(prev);
  for (const row of rows) next.set(row.id, snapshotJob(prev, row));
  return storeJobs(next);
}

function refreshJobs(prev: Map<string, JobRecord>, rows: JobRecord[]): Map<string, JobRecord> {
  const incoming = new Map(rows.map((row) => [row.id, snapshotJob(prev, row)]));
  const next = new Map<string, JobRecord>();
  for (const job of incoming.values()) {
    if (!prev.has(job.id)) next.set(job.id, job);
  }
  for (const [id, job] of prev) next.set(id, incoming.get(id) ?? job);
  return storeJobs(next);
}

function takeJobs(source: Iterable<JobRecord>, count: number): Map<string, JobRecord> {
  const next = new Map<string, JobRecord>();
  for (const job of source) {
    next.set(job.id, job);
    if (next.size >= count) break;
  }
  return next;
}

function historyCursor(jobs: Map<string, JobRecord>): { before: string; before_id: string } | null {
  let oldest: JobRecord | null = null;
  for (const job of jobs.values()) {
    if (!job.created_at) continue;
    if (!oldest?.created_at) {
      oldest = job;
      continue;
    }
    const jobTime = Date.parse(job.created_at);
    const oldestTime = Date.parse(oldest.created_at);
    if (jobTime < oldestTime || (jobTime === oldestTime && job.id < oldest.id)) oldest = job;
  }
  if (!oldest?.created_at) return null;
  return { before: oldest.created_at, before_id: oldest.id };
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
  const [jobs, setJobs] = useState<Map<string, JobRecord>>(() => takeJobs(rememberedJobs.values(), HISTORY_PAGE));
  const [maxUploadMb, setMaxUploadMb] = useState<number | null>(null);
  const [error, setError] = useState<{ code?: string; detail?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [historyReady, setHistoryReady] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const mounted = useRef(true);
  const jobsRef = useRef(jobs);
  jobsRef.current = jobs;
  const hasMoreRef = useRef(true);
  const loadingRef = useRef(false);
  const sentinelRef = useRef<HTMLLIElement | null>(null);
  const loadRef = useRef<(mode: "replace" | "append" | "refresh") => Promise<void>>(async () => undefined);

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

  async function loadHistory(mode: "replace" | "append" | "refresh") {
    if (!mounted.current || loadingRef.current) return;
    if (mode === "append" && !hasMoreRef.current) return;
    const params = new URLSearchParams({ limit: String(HISTORY_PAGE) });
    if (mode === "append") {
      const cursor = historyCursor(jobsRef.current);
      if (!cursor) {
        hasMoreRef.current = false;
        setHasMore(false);
        return;
      }
      params.set("before", cursor.before);
      params.set("before_id", cursor.before_id);
    }
    loadingRef.current = true;
    if (mode === "append") setLoadingMore(true);
    try {
      const rows = await request<JobRecord[]>(`/api/jobs?${params.toString()}`);
      if (!mounted.current) return;
      const known = new Set(jobsRef.current.keys());
      const added = rows.reduce((count, row) => count + (known.has(row.id) ? 0 : 1), 0);
      setJobs((prev) => {
        const next = mode === "append" ? appendJobs(prev, rows) : mode === "refresh" ? refreshJobs(prev, rows) : adoptJobs(prev, rows);
        jobsRef.current = next;
        return next;
      });
      if (mode !== "refresh") {
        const more = rows.length === HISTORY_PAGE && (mode === "replace" || added > 0);
        hasMoreRef.current = more;
        setHasMore(more);
      }
    } catch {
      if (mode === "append" && mounted.current) {
        hasMoreRef.current = false;
        setHasMore(false);
      }
    } finally {
      loadingRef.current = false;
      if (mounted.current) {
        setHistoryReady(true);
        setLoadingMore(false);
      }
    }
  }

  loadRef.current = loadHistory;

  useEffect(() => {
    void loadRef.current("replace");
  }, []);

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node || !hasMore || !historyReady) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void loadRef.current("append");
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasMore, historyReady, jobs.size]);

  useEventStream({
    fetchState: () => loadRef.current("refresh"),
    onMessage: (msg) => {
      if (msg.type !== "job") return;
      const existing = jobsRef.current.get(msg.id);
      if (!existing) {
        void loadRef.current("refresh");
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
        {historyReady && history.length === 0 && <li className="px-4 py-6 text-sm text-stark-gray">No translations yet.</li>}
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
        {historyReady && hasMore && history.length > 0 && (
          <li ref={sentinelRef} className="px-4 py-3 text-center text-xs uppercase tracking-[0.16em] text-stark-gray" aria-live="polite">
            {loadingMore ? "Loading" : ""}
          </li>
        )}
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
    void refreshDocument().catch(() => undefined);
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
  const analyzing = document?.analysis_status !== "ready" && document?.analysis_status !== "failed";
  const heading = document?.analysis_status === "failed" ? "Analysis failed" : analyzing ? "Analyzing the document" : "Price before you translate";
  return (
    <Shell>
      <p className="text-xs uppercase tracking-[0.22em] text-stark-gray">{document?.filename || "Analyzing"}</p>
      <h1 className="mt-2 text-3xl font-semibold">{heading}</h1>
      {analyzing && (
        <div className="mt-6" role="status" aria-live="polite" aria-busy="true">
          <p className="text-xs uppercase tracking-[0.22em] text-stark-gold">Analyzing</p>
          <div className="mt-4 h-2 overflow-hidden bg-white/10" role="progressbar" aria-label="Analyzing the document">
            <div className="h-2 w-2/5 animate-indeterminate bg-stark-gold" />
          </div>
          <p className="mt-3 text-sm text-stark-gray">Reading pages and classifying each one.</p>
        </div>
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

function adoptJob(current: JobRecord | null, incoming: JobRecord, pending: JobMessage | null): JobRecord {
  const base = mergeJobRecord(current, incoming);
  if (!pending || pending.id !== incoming.id) return base;
  const merged = mergeState(base, pending);
  return {
    ...merged,
    done_segments: Math.max(base.done_segments || 0, merged.done_segments || 0),
    total_segments: Math.max(base.total_segments || 0, merged.total_segments || 0),
    cost_cents: Math.max(base.cost_cents || 0, merged.cost_cents || 0),
  };
}

function applyJobEvent(current: JobRecord, msg: JobMessage): JobRecord {
  const merged = mergeState(current, msg);
  if (merged === current) return current;
  return {
    ...merged,
    done_segments: Math.max(current.done_segments || 0, merged.done_segments || 0),
    total_segments: Math.max(current.total_segments || 0, merged.total_segments || 0),
    cost_cents: Math.max(current.cost_cents || 0, merged.cost_cents || 0),
  };
}

function pendingEdits(segments: SegmentRecord[], drafts: Record<number, string>) {
  const edits: { block_idx: number; text: string }[] = [];
  for (const segment of segments) {
    for (const block of segment.blocks) {
      if (block.kind === "image") continue;
      const draft = drafts[block.block_idx];
      if (draft === undefined || draft === (block.target ?? "")) continue;
      edits.push({ block_idx: block.block_idx, text: draft });
    }
  }
  return edits;
}

function JobPage() {
  const { id } = useParams();
  const [job, setJob] = useState<JobRecord | null>(() => (id ? rememberedJobs.get(id) ?? null : null));
  const [segments, setSegments] = useState<SegmentRecord[]>([]);
  const [cost, setCost] = useState<CostReport | null>(null);
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [retrying, setRetrying] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [compared, setCompared] = useState(false);
  const idRef = useRef(id);
  idRef.current = id;
  const jobRef = useRef(job);
  jobRef.current = job;
  const pendingEvent = useRef<JobMessage | null>(null);
  const jobGen = useRef(0);
  const appliedGen = useRef(0);
  const detailsFlight = useRef<string | null>(null);
  const comparedRef = useRef(false);
  comparedRef.current = compared;

  useEffect(() => {
    setJob(id ? rememberedJobs.get(id) ?? null : null);
    setSegments([]);
    setCost(null);
    setDrafts({});
    setRetrying(false);
    setSaving(false);
    setSavedNote(null);
    setActionError(null);
    setCompared(false);
    pendingEvent.current = null;
    jobGen.current = 0;
    appliedGen.current = 0;
    detailsFlight.current = null;
    void refreshJob();
  }, [id]);

  useEffect(() => {
    if (!id) return;
    const timer = setInterval(() => {
      const status = jobRef.current?.status;
      if (status && isTerminal("job", status) && (status !== "done" || comparedRef.current)) return;
      void refreshJob();
    }, 1000);
    return () => clearInterval(timer);
  }, [id]);

  async function loadDetails(jobId: string) {
    if (detailsFlight.current === jobId) return;
    detailsFlight.current = jobId;
    try {
      const [nextSegments, nextCost] = await Promise.all([
        request<SegmentRecord[]>(`/api/jobs/${jobId}/segments`),
        request<CostReport>(`/api/jobs/${jobId}/cost`),
      ]);
      if (idRef.current !== jobId) return;
      setSegments(nextSegments);
      setCost(nextCost);
      setCompared(true);
    } finally {
      if (detailsFlight.current === jobId) detailsFlight.current = null;
    }
  }

  async function refreshJob() {
    const jobId = idRef.current;
    if (!jobId) return;
    const gen = ++jobGen.current;
    try {
      const next = await request<JobRecord>(`/api/jobs/${jobId}`);
      if (idRef.current !== jobId || gen < appliedGen.current) return;
      appliedGen.current = gen;
      const pending = pendingEvent.current?.id === jobId ? pendingEvent.current : null;
      pendingEvent.current = null;
      setJob((current) => adoptJob(current, next, pending));
      if (next.status === "done" || pending?.status === "done") await loadDetails(jobId);
    } catch {
      return;
    }
  }

  useEventStream({
    fetchState: refreshJob,
    onMessage: (msg) => {
      if (msg.type !== "job" || msg.id !== idRef.current) return;
      setJob((current) => {
        if (!current) {
          pendingEvent.current = msg;
          return current;
        }
        return applyJobEvent(current, msg);
      });
      if (isTerminal("job", msg.status)) void refreshJob();
    },
  });

  async function retry() {
    const jobId = idRef.current;
    if (!jobId) return;
    setRetrying(true);
    setActionError(null);
    setCompared(false);
    setSegments([]);
    try {
      const next = await request<JobRecord>(`/api/jobs/${jobId}/retry`, { method: "POST" });
      const resumed = { ...next, seq: Date.now() };
      setJob(resumed);
      rememberJob(resumed);
    } catch (err) {
      const failure = readFailure(err);
      setActionError(errorMessage(failure.code, failure.detail));
    } finally {
      setRetrying(false);
    }
  }

  const edits = pendingEdits(segments, drafts);

  async function saveEdits() {
    const jobId = idRef.current;
    const snapshot = pendingEdits(segments, drafts);
    if (!jobId || snapshot.length === 0) return;
    setSaving(true);
    setActionError(null);
    setSavedNote(null);
    try {
      await request(`/api/jobs/${jobId}/edits`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ edits: snapshot }),
      });
      const saved = new Map(snapshot.map((edit) => [edit.block_idx, edit.text]));
      setSegments((current) =>
        current.map((segment) => ({
          ...segment,
          blocks: segment.blocks.map((block) => {
            const text = saved.get(block.block_idx);
            return text === undefined ? block : { ...block, target: text, origin: "user" };
          }),
        })),
      );
      setDrafts((current) => {
        const next = { ...current };
        for (const edit of snapshot) {
          if (next[edit.block_idx] === edit.text) delete next[edit.block_idx];
        }
        return next;
      });
      setSavedNote("Changes saved. Download the file again to include them.");
      await refreshJob();
    } catch (err) {
      const failure = readFailure(err);
      setActionError(errorMessage(failure.code, failure.detail));
    } finally {
      setSaving(false);
    }
  }

  const ratio = job ? progressRatio(job.done_segments, job.total_segments, job.status) : 6;
  const label = job ? progressLabel(job.done_segments, job.total_segments, job.status) : "Loading";
  const showCompare = job?.status === "done" && compared;
  const showLoader = !job || (job.status !== "failed" && job.status !== "cancelled" && !showCompare);
  return (
    <Shell>
      {job?.document_filename && (
        <h1 className="truncate text-2xl font-semibold" title={job.document_filename}>{job.document_filename}</h1>
      )}
      <p className={`${job?.document_filename ? "mt-2" : ""} text-xs uppercase tracking-[0.22em] text-stark-gold`}>
        {label}
      </p>
      <div className="mt-4 h-1 overflow-hidden bg-white/10" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={ratio} aria-label={label}>
        <div className="h-1 bg-stark-gold transition-[width] duration-500 ease-out" style={{ width: `${ratio}%` }} />
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
      {actionError && job?.status !== "done" && <p className="mt-3 text-sm text-stark-gold" role="alert">{actionError}</p>}
      {showCompare && cost && (
        <ul className="mt-6 grid gap-2 sm:grid-cols-3">
          {cost.stages.map((stage) => (
            <li key={stage.stage} className="border border-white/10 p-3 text-sm">
              <div className="text-xs uppercase tracking-[0.16em] text-stark-gray">{stage.stage}</div>
              <div className="mt-1">{stage.tokens_in + stage.tokens_out} tokens · ${(stage.cost_cents / 100).toFixed(4)}</div>
            </li>
          ))}
        </ul>
      )}
      {showLoader && (
        <div className="mt-8 border border-white/10 px-6 py-12" role="status" aria-live="polite" aria-busy="true">
          <div className="flex items-center gap-4">
            <span className="h-5 w-5 shrink-0 animate-spin rounded-full border-2 border-white/15 border-t-stark-gold" />
            <div>
              <p className="text-xs uppercase tracking-[0.18em] text-stark-gold">{job?.status || "Loading"}</p>
              <p className="mt-2 text-sm text-stark-gray">{label}</p>
            </div>
          </div>
        </div>
      )}
      {showCompare && (
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
                    onChange={(event) => {
                      const value = event.target.value;
                      setSavedNote(null);
                      setDrafts((current) => ({ ...current, [block.block_idx]: value }));
                    }}
                  />
                </div>
              </div>
            ))}
          </article>
        ))}
      </div>
      )}
      {showCompare && (
        <div className="mt-8 flex flex-wrap items-center gap-4">
          <button
            type="button"
            disabled={saving || edits.length === 0}
            onClick={() => void saveEdits()}
            className="bg-stark-gold px-5 py-2 text-sm font-semibold uppercase tracking-[0.16em] text-black disabled:opacity-50"
          >
            {saving ? "Saving" : "Save changes"}
          </button>
          <a className="inline-block border border-stark-gold px-4 py-2 text-sm uppercase tracking-[0.16em] text-stark-gold" href={`/api/jobs/${id}/download`}>Download</a>
          {savedNote && <p className="text-sm text-stark-gray">{savedNote}</p>}
          {actionError && <p className="text-sm text-stark-gold" role="alert">{actionError}</p>}
        </div>
      )}
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

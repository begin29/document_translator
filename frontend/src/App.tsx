import { FormEvent, ReactNode, useEffect, useState } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api, DocumentRecord, JobRecord, SegmentRecord } from "./api";
import { errorMessage, progressLabel } from "./copy";

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-stark-black text-stark-white">
      <header className="border-b border-white/10">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
          <Link to="/" className="flex items-center gap-3">
            <span className="grid h-9 w-9 place-items-center rounded-full border border-stark-gold text-xs font-semibold tracking-widest text-stark-gold">SF</span>
            <span className="text-sm font-semibold tracking-[0.35em]">STARK</span>
          </Link>
          <nav className="text-xs uppercase tracking-[0.2em] text-stark-gray">
            <Link to="/">Translator</Link>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-10">{children}</main>
    </div>
  );
}

function ErrorPanel({ code, detail }: { code?: string | null; detail?: string | null }) {
  if (!code && !detail) return null;
  return (
    <div className="border border-stark-gold/60 bg-stark-ink px-4 py-3 text-sm text-stark-gold" role="alert">
      <div className="text-xs uppercase tracking-[0.18em]">Needs attention</div>
      <p className="mt-1 text-stark-white">{errorMessage(code, detail)}</p>
      {code && <p className="mt-1 font-mono text-xs text-stark-gray">{code}</p>}
    </div>
  );
}

function Home() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [error, setError] = useState<{ code?: string; detail?: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<JobRecord[]>("/api/jobs").then(setJobs).catch(() => setJobs([]));
  }, []);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = event.currentTarget.elements.namedItem("file") as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    if (file.size > 40 * 1024 * 1024) {
      setError({ code: "file_too_large", detail: "Client limit is 40 MB." });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const body = new FormData();
      body.append("file", file);
      const document = await api<DocumentRecord>("/api/documents", { method: "POST", body });
      navigate(`/documents/${document.id}`);
    } catch (err) {
      const known = err as Error & { code?: string; detail?: string };
      setError({ code: known.code, detail: known.message });
    } finally {
      setBusy(false);
    }
  }

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
        {jobs.length === 0 && <li className="px-4 py-6 text-sm text-stark-gray">No translations yet.</li>}
        {jobs.map((job) => (
          <li key={job.id} className="flex items-center justify-between px-4 py-3 text-sm">
            <Link to={`/jobs/${job.id}`} className="hover:text-stark-gold">{job.target_lang.toUpperCase()} · {job.model}</Link>
            <span className="text-stark-gray">{job.status}</span>
          </li>
        ))}
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
  const [mode, setMode] = useState("reflow");
  const [languages, setLanguages] = useState<{ code: string; name: string }[]>([]);
  const [error, setError] = useState<{ code?: string; detail?: string } | null>(null);

  useEffect(() => {
    api<{ code: string; name: string }[]>("/api/meta/languages").then(setLanguages).catch(() => setLanguages([]));
  }, []);

  useEffect(() => {
    let stop = false;
    async function poll() {
      while (!stop) {
        const next = await api<DocumentRecord>(`/api/documents/${id}`);
        if (stop) return;
        setDocument(next);
        if (next.analysis_status === "ready" || next.analysis_status === "failed") return;
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
    poll().catch((err: Error & { code?: string }) => setError({ code: err.code, detail: err.message }));
    return () => {
      stop = true;
    };
  }, [id]);

  async function translate() {
    setError(null);
    try {
      const job = await api<JobRecord>("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ document_id: id, target_lang: target, model, render_mode: mode }),
      });
      navigate(`/jobs/${job.id}`);
    } catch (err) {
      const known = err as Error & { code?: string };
      setError({ code: known.code, detail: known.message });
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
              {document.analysis.models.map((item) => (
                <label key={item.model} className={`block border p-3 text-sm ${model === item.model ? "border-stark-gold" : "border-white/10"}`}>
                  <input type="radio" name="model" className="mr-2" checked={model === item.model} onChange={() => setModel(item.model)} />
                  {item.model} · {item.tier}
                  <div className="mt-1 text-stark-gray">{item.segments} segments · ~${(item.cost_cents / 100).toFixed(4)} · OCR ${(item.ocr_cost_cents / 100).toFixed(4)} · ~{item.duration_seconds}s</div>
                </label>
              ))}
            </div>
            <label className="mt-4 block text-xs uppercase tracking-[0.16em] text-stark-gray">PDF layout
              <select value={mode} onChange={(event) => setMode(event.target.value)} className="mt-2 w-full bg-black p-2 text-sm normal-case tracking-normal">
                <option value="reflow">Reflow</option>
                <option value="layout">Preserve layout (beta)</option>
              </select>
            </label>
            <button onClick={translate} className="mt-5 bg-stark-gold px-5 py-2 text-sm font-semibold uppercase tracking-[0.16em] text-black">Translate</button>
            {error && <div className="mt-4"><ErrorPanel code={error.code} detail={error.detail} /></div>}
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
  const [cost, setCost] = useState<{ stages: { stage: string; cost_cents: number; tokens_in: number; tokens_out: number }[] } | null>(null);
  const [drafts, setDrafts] = useState<Record<number, string>>({});

  useEffect(() => {
    let stop = false;
    async function poll() {
      while (!stop) {
        const next = await api<JobRecord>(`/api/jobs/${id}`);
        if (stop) return;
        setJob(next);
        if (["translating", "reviewing", "rendering", "done", "failed"].includes(next.status)) {
          setSegments(await api<SegmentRecord[]>(`/api/jobs/${id}/segments`));
          setCost(await api(`/api/jobs/${id}/cost`));
        }
        if (["done", "failed", "cancelled"].includes(next.status)) return;
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
    poll().catch(() => undefined);
    return () => {
      stop = true;
    };
  }, [id]);

  async function save(blockIdx: number) {
    await api(`/api/jobs/${id}/segments/0`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ block_idx: blockIdx, text: drafts[blockIdx] }),
    });
    setSegments(await api<SegmentRecord[]>(`/api/jobs/${id}/segments`));
  }

  const ratio = job && job.total_segments ? Math.round((job.done_segments / job.total_segments) * 100) : job?.status === "done" ? 100 : 8;
  return (
    <Shell>
      <p className="text-xs uppercase tracking-[0.22em] text-stark-gold">{job ? progressLabel(job.done_segments, job.total_segments, job.status) : "Loading"}</p>
      <div className="mt-4 h-1 bg-white/10">
        <div className="h-1 bg-stark-gold" style={{ width: `${Math.min(ratio, 100)}%` }} />
      </div>
      {job && <p className="mt-3 text-sm text-stark-gray">Spent ${(job.cost_cents / 100).toFixed(4)} · {job.model} · {job.target_lang}</p>}
      {job?.quality_warning && <p className="mt-3 text-sm text-stark-gold">A reviewer retranslated at least one segment because the first pass failed a quality check.</p>}
      {job?.error_code && <div className="mt-4"><ErrorPanel code={job.error_code} detail={job.error_detail} /></div>}
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

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/documents/:id" element={<DocumentPage />} />
      <Route path="/jobs/:id" element={<JobPage />} />
    </Routes>
  );
}

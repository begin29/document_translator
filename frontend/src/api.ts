export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const error = new Error(body?.message || response.statusText) as Error & { code?: string; detail?: string };
    error.code = body?.code;
    error.detail = body?.detail;
    throw error;
  }
  return body as T;
}

export type DocumentRecord = {
  id: string;
  filename: string;
  format: string;
  size_bytes: number;
  page_count: number | null;
  char_count: number | null;
  detected_source_lang: string | null;
  analysis_status: string;
  analysis: {
    pages: { page: number; class: string; route: string; thumbnail?: string | null }[];
    class_counts: Record<string, number>;
    class_lines?: { class: string; pages: number; action: string; cost_cents: number; warning: string | null }[];
    models: {
      model: string;
      tier: string;
      tokens: number;
      segments: number;
      cost_cents: number;
      ocr_cost_cents: number;
      duration_seconds: number;
    }[];
    ocr_pages: number[];
  } | null;
  error_code: string | null;
  error_detail: string | null;
};

export type JobRecord = {
  id: string;
  document_id: string;
  source_lang: string | null;
  target_lang: string;
  model: string;
  status: string;
  total_segments: number;
  done_segments: number;
  cost_cents: number;
  error_code: string | null;
  error_detail: string | null;
  output_stale: boolean;
  quality_warning: boolean;
  render_mode: string;
};

export type SegmentRecord = {
  idx: number;
  status: string;
  tokens_in: number;
  review_state: string | null;
  blocks: { block_idx: number; kind: string; page: number; source: string | null; target: string | null; origin: string | null }[];
};

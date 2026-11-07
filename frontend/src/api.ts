export class ApiError extends Error {
  code: string;
  detail: string;
  status: number;

  constructor(code: string, message: string, detail: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.detail = detail;
    this.status = status;
  }
}

function nonJsonError(response: Response): ApiError {
  if (response.status === 413) {
    return new ApiError("file_too_large", "This file is larger than the upload limit.", response.statusText, response.status);
  }
  if (response.status === 502 || response.status === 503 || response.status === 504) {
    return new ApiError("service_unavailable", response.statusText || "Service unavailable", response.statusText, response.status);
  }
  return new ApiError(`http_${response.status}`, response.statusText || `HTTP ${response.status}`, response.statusText, response.status);
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const text = await response.text();
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    if (!response.ok) throw nonJsonError(response);
    throw new ApiError("invalid_response", "The server returned a response that could not be read.", response.statusText, response.status);
  }
  if (!response.ok) {
    const record = body && typeof body === "object" ? (body as { code?: string; message?: string; detail?: string }) : {};
    throw new ApiError(
      record.code || `http_${response.status}`,
      record.message || response.statusText || `HTTP ${response.status}`,
      record.detail || "",
      response.status,
    );
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
  seq?: number;
};

export type JobRecord = {
  id: string;
  document_id: string;
  document_filename: string | null;
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
  created_at: string | null;
  seq?: number;
};

export type JobMessage = {
  type: "job";
  id: string;
  status: string;
  done_segments: number;
  total_segments: number;
  cost_cents: number;
  error_code: string | null;
  seq: number;
};

export type DocumentMessage = {
  type: "document";
  id: string;
  analysis_status: string;
  error_code: string | null;
  seq: number;
};

export type SegmentRecord = {
  idx: number;
  status: string;
  tokens_in: number;
  review_state: string | null;
  blocks: { block_idx: number; kind: string; page: number; source: string | null; target: string | null; origin: string | null }[];
};

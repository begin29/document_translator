export const ERROR_COPY: Record<string, string> = {
  corrupt_pdf: "This PDF is damaged and could not be opened.",
  corrupt_file: "This file is damaged and could not be opened.",
  encrypted_pdf: "This PDF is encrypted. Remove the password and upload it again.",
  empty_document: "This document has no extractable text.",
  unsupported_type: "This file type is not supported. Upload a PDF, DOCX, PNG, or JPEG.",
  file_too_large: "This file is larger than the upload limit.",
  invalid_file: "This file is empty or its contents do not match its name.",
  not_a_document: "No readable document content was found in this upload.",
  ocr_no_text: "No readable text was found on the scanned pages.",
  provider_timeout: "The translation provider timed out. The job stopped after the retry limit.",
  provider_error: "The translation provider returned an error. The job stopped after the retry limit.",
  rate_limited: "The translation provider asked us to slow down.",
  cost_cap_exceeded: "This job hit its cost cap and stopped before further provider calls.",
  analysis_failed: "Analysis failed before a price could be shown.",
  render_failed: "The translated file could not be rendered.",
  not_ready: "This step is still running.",
  unknown_model: "That model is not available.",
  unknown_language: "That target language is not supported.",
  cancelled: "This translation was cancelled.",
  service_unavailable: "The translator is temporarily unavailable. Try again in a moment.",
  invalid_response: "The server returned a response that could not be read.",
  path_outside_workspace: "That file path is outside the workspace.",
  not_found: "That document or translation could not be found.",
  unauthorized: "Sign in to continue.",
  csrf_failed: "The request was rejected. Refresh the page and try again.",
  too_many_attempts: "Too many attempts. Wait a few minutes and try again.",
  signup_disabled: "Sign up is disabled.",
  email_taken: "An account with that email already exists.",
  invalid_credentials: "The email or password is incorrect.",
  weak_password: "Password must be at least 10 characters.",
  invalid_email: "Enter a valid email address.",
};

export function errorMessage(code: string | null | undefined, fallback?: string | null): string {
  if (code && ERROR_COPY[code]) return ERROR_COPY[code];
  if (fallback) return fallback;
  if (code) return `Stopped with code ${code}.`;
  return "The request did not complete.";
}

export function progressLabel(done: number, total: number, status: string): string {
  if (status === "done") return "Translation complete";
  if (status === "failed") return "Translation failed";
  if (status === "cancelled") return "Translation cancelled";
  if (!total) return `Working · ${status}`;
  const pct = Math.round((done / total) * 100);
  return `${status} · ${done}/${total} segments · ${pct}%`;
}

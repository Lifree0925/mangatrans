/** 与后端 Schema 对齐的基础类型。 */

export interface Project {
  id: string;
  name: string;
  source_language: string;
  target_language: string;
  active_source_document_id: string | null;
  storage_path: string | null;
  created_at: string;
  updated_at: string;
}

export interface Chapter {
  id: string;
  chapter_no: number;
  title: string | null;
  source_start: number;
  source_end: number;
}

export interface Scene {
  id: string;
  chapter_id: string;
  scene_no: number;
  heading: string | null;
  source_start: number;
  source_end: number;
}

export interface Segment {
  id: string;
  chapter_id: string | null;
  scene_id: string | null;
  segment_no: number;
  segment_type: "heading" | "dialogue" | "narrative" | "other";
  speaker_name: string | null;
  source_text: string;
  source_start: number;
  source_end: number;
}

export interface ParseReport {
  total_chars: number;
  covered_chars: number;
  uncovered_chars: number;
  chapter_count: number;
  scene_count: number;
  dialogue_count: number;
  narrative_count: number;
  heading_count: number;
  other_count: number;
  anomalies: string[];
  parser_version: string;
  coverage_ok: boolean;
}

export interface TaskInfo {
  id: string;
  task_type: string;
  status: string;
  progress: Record<string, unknown>;
  error_message: string | null;
}

export interface ProviderInfo {
  id: string;
  display_name: string;
  base_url: string;
  model_name: string;
  credential_ref: string;
  has_credential: boolean;
  masked_key: string | null;
  is_default: boolean;
  is_verified: boolean;
  last_tested_at: string | null;
  last_test_error: string | null;
  config_version: number;
}

export interface QAIssueInfo {
  id: string;
  issue_type: string;
  severity: string;
  source_quote: string;
  target_quote: string;
  explanation: string;
  status: string;
}

export interface ExportResult {
  filename: string;
  format: "md" | "txt" | "docx";
  content: string | null;
  content_base64: string | null;
  report: { segments: number; untranslated: number; issues_total: number; high_critical_issues: number };
}

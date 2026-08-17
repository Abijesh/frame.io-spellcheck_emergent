// Mirrors backend/models.py. Keep in sync when the API shape changes.

export type IssueType = "spelling" | "grammar" | "punctuation" | "capitalization";
export type Severity = "low" | "medium" | "high";

export interface Issue {
  id: string;
  timestamp_sec: number;
  end_sec: number | null;
  type: IssueType;
  original: string;
  suggestion: string;
  explanation: string;
  source_text: string;
  severity: Severity;
  thumbnail_b64: string | null;
  posted_to_frameio: boolean;
  frameio_comment_id: string | null;
}

export type AnalysisStatus =
  | "queued"
  | "downloading"
  | "extracting"
  | "analyzing"
  | "posting"
  | "done"
  | "failed";

export interface Analysis {
  id: string;
  created_at: string;
  updated_at: string;
  frameio_url: string | null;
  frameio_asset_id: string | null;
  video_filename: string | null;
  transcript: string | null;
  auto_post: boolean;
  password_required: boolean;
  status: AnalysisStatus;
  progress: number;
  message: string;
  total_frames: number;
  analyzed_frames: number;
  duration_sec: number;
  video_fps: number;
  issues: Issue[];
  posted_count: number;
  post_error: string | null;
  error: string | null;
}

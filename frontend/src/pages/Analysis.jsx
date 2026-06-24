import { useEffect, useState, useCallback } from "react";
import { useParams, Link } from "react-router-dom";
import { toast } from "sonner";
import {
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Send,
  Clock,
  ArrowLeft,
  Loader2,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Separator } from "@/components/ui/separator";
import { getAnalysis, postComments } from "@/lib/api";

const formatTime = (sec) => {
  if (sec === undefined || sec === null || sec < 0) return "Script";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
};

const SEVERITY_COLOR = {
  high: "border-rose-700 text-rose-500",
  medium: "border-amber-700 text-amber-500",
  low: "border-zinc-700 text-zinc-400",
};

const TYPE_COLOR = {
  spelling: "bg-rose-600/10 text-rose-500 border-rose-700",
  grammar: "bg-amber-600/10 text-amber-500 border-amber-700",
  punctuation: "bg-zinc-700/30 text-zinc-300 border-zinc-700",
  capitalization: "bg-emerald-600/10 text-emerald-500 border-emerald-700",
};

const StatusBadge = ({ status }) => {
  const map = {
    queued: { label: "Queued", color: "border-zinc-700 text-zinc-400" },
    downloading: { label: "Downloading", color: "border-blue-700 text-blue-400" },
    extracting: { label: "Extracting", color: "border-blue-700 text-blue-400" },
    analyzing: { label: "Analyzing", color: "border-rose-700 text-rose-400" },
    posting: { label: "Posting", color: "border-amber-700 text-amber-400" },
    done: { label: "Done", color: "border-emerald-700 text-emerald-400" },
    failed: { label: "Failed", color: "border-rose-700 text-rose-500" },
  };
  const c = map[status] || map.queued;
  return (
    <span
      data-testid="status-badge"
      className={`font-mono-tech text-[10px] uppercase tracking-widest px-2 py-1 border ${c.color}`}
    >
      {c.label}
    </span>
  );
};

export default function Analysis() {
  const { id } = useParams();
  const [analysis, setAnalysis] = useState(null);
  const [posting, setPosting] = useState(false);

  const load = useCallback(async () => {
    try {
      const a = await getAnalysis(id);
      setAnalysis(a);
      return a;
    } catch (e) {
      toast.error("Could not load analysis");
      return null;
    }
  }, [id]);

  useEffect(() => {
    let mounted = true;
    let timer;
    const tick = async () => {
      const a = await load();
      if (!mounted) return;
      if (a && a.status !== "done" && a.status !== "failed") {
        timer = setTimeout(tick, 2000);
      }
    };
    tick();
    return () => {
      mounted = false;
      if (timer) clearTimeout(timer);
    };
  }, [load]);

  const onPost = async () => {
    setPosting(true);
    try {
      const res = await postComments(id);
      toast.success(`Posted ${res.posted} new comment(s) to Frame.io`);
      await load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Failed to post comments");
    } finally {
      setPosting(false);
    }
  };

  if (!analysis) {
    return (
      <div className="min-h-[60vh] flex items-center justify-center" data-testid="analysis-loading">
        <Loader2 className="w-6 h-6 animate-spin text-zinc-600" />
      </div>
    );
  }

  const issues = analysis.issues || [];
  const isRunning = analysis.status !== "done" && analysis.status !== "failed";
  const unposted = issues.filter((i) => !i.posted_to_frameio).length;

  return (
    <main data-testid="analysis-page" className="max-w-7xl mx-auto px-6 py-12">
      <Link
        to="/"
        className="inline-flex items-center text-sm text-zinc-500 hover:text-white transition-colors mb-6"
        data-testid="back-link"
      >
        <ArrowLeft className="w-4 h-4 mr-1" /> Back
      </Link>

      {/* Header */}
      <div className="border border-zinc-800 bg-zinc-950 p-8 mb-6">
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div className="space-y-3">
            <div className="flex items-center gap-3 flex-wrap">
              <StatusBadge status={analysis.status} />
              <span className="font-mono-tech text-xs text-zinc-500">
                ID · {analysis.id.slice(0, 8)}
              </span>
              {analysis.frameio_asset_id && (
                <span className="font-mono-tech text-xs text-zinc-500">
                  ASSET · {analysis.frameio_asset_id.slice(0, 8)}
                </span>
              )}
            </div>
            <h1
              data-testid="analysis-title"
              className="font-display font-black text-3xl tracking-tighter"
            >
              {analysis.video_filename ||
                analysis.frameio_url ||
                "Untitled review"}
            </h1>
            <p className="text-sm text-zinc-500" data-testid="analysis-message">
              {analysis.message || "Processing..."}
            </p>
          </div>

          <div className="flex items-center gap-2">
            {analysis.status === "done" && unposted > 0 && (
              <Button
                onClick={onPost}
                disabled={posting}
                data-testid="post-comments-btn"
                className="bg-rose-600 hover:bg-rose-500 text-white rounded-none font-mono-tech text-xs uppercase tracking-widest"
              >
                <Send className="w-4 h-4 mr-2" />
                {posting ? "Posting..." : `Post ${unposted} to Frame.io`}
              </Button>
            )}
          </div>
        </div>

        <Separator className="my-6 bg-zinc-900" />

        {/* Stats grid */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-px bg-zinc-800 border border-zinc-800">
          <Stat label="Frames" value={`${analysis.analyzed_frames}/${analysis.total_frames || "—"}`} />
          <Stat label="Duration" value={analysis.duration_sec ? `${Math.round(analysis.duration_sec)}s` : "—"} />
          <Stat label="Issues" value={issues.length} accent={issues.length > 0} />
          <Stat label="Posted" value={analysis.posted_count || 0} />
        </div>

        {isRunning && (
          <div className="mt-6 space-y-2">
            <div className="flex justify-between text-xs font-mono-tech text-zinc-500">
              <span>PROGRESS</span>
              <span>{analysis.progress}%</span>
            </div>
            <Progress
              value={analysis.progress}
              className="h-1 bg-zinc-900"
              data-testid="progress-bar"
            />
          </div>
        )}

        {analysis.status === "failed" && (
          <div
            data-testid="analysis-error"
            className="mt-6 border border-rose-900 bg-rose-950/30 p-4 flex items-start gap-3"
          >
            <XCircle className="w-5 h-5 text-rose-500 mt-0.5 shrink-0" />
            <div>
              <div className="font-display font-bold text-rose-500 mb-1">
                Analysis failed
              </div>
              <p className="text-sm text-zinc-400">{analysis.error}</p>
            </div>
          </div>
        )}
      </div>

      {/* Issues */}
      <div className="border border-zinc-800 bg-zinc-950">
        <div className="flex items-center justify-between p-6 border-b border-zinc-900">
          <h2 className="font-display font-bold text-xl tracking-tight">
            Detected issues
          </h2>
          <span className="font-mono-tech text-xs text-zinc-500">
            {issues.length} TOTAL
          </span>
        </div>

        {issues.length === 0 ? (
          <div
            className="p-16 text-center text-zinc-600 flex flex-col items-center gap-3"
            data-testid="no-issues"
          >
            {analysis.status === "done" ? (
              <>
                <CheckCircle2 className="w-10 h-10 text-emerald-500" />
                <p className="text-zinc-400">
                  No spelling or grammar issues found. Ship it.
                </p>
              </>
            ) : (
              <>
                <Loader2 className="w-6 h-6 animate-spin" />
                <p>Issues will appear here as frames are analyzed...</p>
              </>
            )}
          </div>
        ) : (
          <div className="divide-y divide-zinc-900">
            {issues
              .slice()
              .sort((a, b) => a.timestamp_sec - b.timestamp_sec)
              .map((issue, idx) => (
                <IssueRow key={issue.id || idx} issue={issue} />
              ))}
          </div>
        )}
      </div>
    </main>
  );
}

const Stat = ({ label, value, accent }) => (
  <div className="bg-zinc-950 p-5">
    <div className="font-mono-tech text-[10px] uppercase tracking-widest text-zinc-600 mb-2">
      {label}
    </div>
    <div
      className={`font-display font-bold text-2xl tracking-tight ${
        accent ? "text-rose-500" : "text-white"
      }`}
    >
      {value}
    </div>
  </div>
);

const IssueRow = ({ issue }) => (
  <div
    data-testid="issue-row"
    className="p-6 hover:bg-zinc-900/30 transition-colors duration-200 flex gap-6"
  >
    <div className="shrink-0 w-20">
      <div className="font-mono-tech text-sm text-rose-500 flex items-center gap-1">
        <Clock className="w-3 h-3" />
        {issue.timestamp_sec >= 0 ? formatTime(issue.timestamp_sec) : "Script"}
      </div>
      <div className="font-mono-tech text-[10px] text-zinc-600 mt-1">
        {issue.timestamp_sec >= 0 ? `${Math.round(issue.timestamp_sec)}s` : "Transcript"}
      </div>
    </div>

    <div className="flex-1 space-y-2 min-w-0">
      <div className="flex items-center gap-2 flex-wrap">
        <Badge
          className={`rounded-none font-mono-tech text-[10px] uppercase tracking-widest border ${
            TYPE_COLOR[issue.type] || TYPE_COLOR.spelling
          }`}
        >
          {issue.type}
        </Badge>
        <span
          className={`font-mono-tech text-[10px] uppercase tracking-widest px-2 py-0.5 border ${
            SEVERITY_COLOR[issue.severity] || SEVERITY_COLOR.medium
          }`}
        >
          {issue.severity}
        </span>
        {issue.posted_to_frameio && (
          <span className="font-mono-tech text-[10px] uppercase tracking-widest text-emerald-500 flex items-center gap-1">
            <CheckCircle2 className="w-3 h-3" /> Posted
          </span>
        )}
      </div>

      <div className="flex items-center gap-3 flex-wrap text-base">
        <span className="line-through text-zinc-500 font-medium break-all">
          {issue.original || "—"}
        </span>
        {issue.suggestion && (
          <>
            <span className="text-zinc-700">→</span>
            <span className="text-white font-medium break-all">
              {issue.suggestion}
            </span>
          </>
        )}
      </div>

      {issue.explanation && (
        <p className="text-sm text-zinc-500 leading-relaxed">
          {issue.explanation}
        </p>
      )}

      {issue.source_text && issue.source_text !== issue.original && (
        <p className="text-xs text-zinc-700 font-mono-tech">
          {`In: "${issue.source_text}"`}
        </p>
      )}
    </div>
  </div>
);

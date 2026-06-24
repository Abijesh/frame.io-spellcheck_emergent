import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Loader2, ArrowRight, Film } from "lucide-react";

import { listAnalyses } from "@/lib/api";

const STATUS_COLOR = {
  done: "border-emerald-700 text-emerald-500",
  failed: "border-rose-700 text-rose-500",
  analyzing: "border-rose-700 text-rose-400",
  extracting: "border-blue-700 text-blue-400",
  downloading: "border-blue-700 text-blue-400",
  posting: "border-amber-700 text-amber-400",
  queued: "border-zinc-700 text-zinc-400",
};

const formatDate = (iso) => {
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
};

export default function History() {
  const [items, setItems] = useState(null);

  useEffect(() => {
    listAnalyses().then(setItems).catch(() => setItems([]));
  }, []);

  if (items === null) {
    return (
      <div className="min-h-[60vh] flex items-center justify-center">
        <Loader2 className="w-6 h-6 animate-spin text-zinc-600" />
      </div>
    );
  }

  return (
    <main data-testid="history-page" className="max-w-7xl mx-auto px-6 py-12">
      <div className="mb-10">
        <span className="font-mono-tech text-xs text-rose-600 uppercase tracking-widest">
          Past reviews
        </span>
        <h1 className="font-display font-black text-4xl tracking-tighter mt-2">
          History
        </h1>
      </div>

      {items.length === 0 ? (
        <div
          data-testid="history-empty"
          className="border border-zinc-800 bg-zinc-950 p-16 text-center text-zinc-500"
        >
          <Film className="w-10 h-10 mx-auto mb-4 text-zinc-700" />
          No reviews yet. Start one from the home page.
        </div>
      ) : (
        <div className="border border-zinc-800 bg-zinc-950 divide-y divide-zinc-900">
          {items.map((a) => (
            <Link
              key={a.id}
              to={`/analysis/${a.id}`}
              data-testid="history-row"
              className="grid grid-cols-12 gap-4 p-6 hover:bg-zinc-900/30 transition-colors duration-200 items-center"
            >
              <div className="col-span-12 md:col-span-5 min-w-0">
                <div className="font-display font-bold tracking-tight truncate">
                  {a.video_filename || a.frameio_url || "Untitled review"}
                </div>
                <div className="font-mono-tech text-xs text-zinc-600 mt-1">
                  {formatDate(a.created_at)}
                </div>
              </div>
              <div className="col-span-4 md:col-span-2">
                <span
                  className={`font-mono-tech text-[10px] uppercase tracking-widest px-2 py-1 border ${
                    STATUS_COLOR[a.status] || STATUS_COLOR.queued
                  }`}
                >
                  {a.status}
                </span>
              </div>
              <div className="col-span-4 md:col-span-2 font-mono-tech text-xs text-zinc-500">
                {a.issue_count} issues
              </div>
              <div className="col-span-4 md:col-span-2 font-mono-tech text-xs text-zinc-500">
                {a.posted_count || 0} posted
              </div>
              <div className="hidden md:flex md:col-span-1 justify-end">
                <ArrowRight className="w-4 h-4 text-zinc-600" />
              </div>
            </Link>
          ))}
        </div>
      )}
    </main>
  );
}

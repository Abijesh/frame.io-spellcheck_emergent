import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { ArrowRight, Link2, Upload, Sparkles, FileText, Zap, Eye, Lock } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { createAnalysis, getConfig } from "@/lib/api";

const Feature = ({ icon: Icon, title, desc, testid }) => (
  <div
    data-testid={testid}
    className="border border-zinc-800 bg-zinc-950 p-8 transition-transform duration-200 hover:-translate-y-1"
  >
    <Icon className="w-6 h-6 text-brand-500 mb-6" strokeWidth={1.5} />
    <h3 className="font-display font-bold text-lg mb-2 tracking-tight">{title}</h3>
    <p className="text-sm text-zinc-500 leading-relaxed">{desc}</p>
  </div>
);

export default function Landing() {
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [transcript, setTranscript] = useState("");
  const [password, setPassword] = useState("");
  const [autoPost, setAutoPost] = useState(true);
  const [videoFile, setVideoFile] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [cfg, setCfg] = useState({
    llm_configured: false,
    guest_name: "Spellchecker",
  });

  useEffect(() => {
    getConfig().then(setCfg).catch(() => {});
  }, []);

  const onSubmit = async (e) => {
    e.preventDefault();
    if (!url && !videoFile) {
      toast.error("Paste a Frame.io URL or upload a video file.");
      return;
    }
    setSubmitting(true);
    try {
      const analysis = await createAnalysis({
        frameioUrl: url || null,
        transcript: transcript || null,
        password: password || null,
        autoPost,
        videoFile,
      });
      toast.success("Analysis started.");
      navigate(`/analysis/${analysis.id}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to start analysis");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main data-testid="landing-page" className="relative">
      {/* HERO */}
      <section className="relative overflow-hidden border-b border-zinc-900">
        <div
          className="absolute inset-0 opacity-25"
          style={{
            backgroundImage:
              "url(https://images.pexels.com/photos/13922614/pexels-photo-13922614.jpeg)",
            backgroundSize: "cover",
            backgroundPosition: "center",
            filter: "saturate(1.2)",
          }}
        />
        <div className="absolute inset-0 bg-gradient-to-b from-black/40 via-black/70 to-black" />

        <div className="relative max-w-7xl mx-auto px-6 pt-24 pb-32">
          <div className="grid lg:grid-cols-12 gap-12 items-start">
            <div className="lg:col-span-7 fade-up">
              <div className="flex items-center gap-3 mb-8">
                <span className="w-2 h-2 bg-brand-500 pulse-dot" />
                <span className="font-mono-tech text-xs text-zinc-500 uppercase tracking-widest">
                  Automated video QA · Gemini 3 Flash
                </span>
              </div>
              <h1 className="font-display font-black text-5xl sm:text-6xl lg:text-7xl tracking-tighter leading-[0.95] mb-8">
                Spot every typo
                <br />
                before your client
                <br />
                <span className="text-brand-500">does.</span>
              </h1>
              <p className="text-zinc-400 text-lg max-w-xl leading-relaxed mb-10">
                Drop in a Frame.io link. We read every on-screen frame, flag spelling
                and grammar mistakes with timestamps, and post them straight back
                as comments — so revisions stop ping-ponging.
              </p>

              <div className="flex flex-wrap gap-6 font-mono-tech text-xs text-zinc-500 uppercase">
                <span className="flex items-center gap-2">
                  <Eye className="w-4 h-4 text-brand-500" /> Frame-by-frame OCR
                </span>
                <span className="flex items-center gap-2">
                  <Sparkles className="w-4 h-4 text-brand-500" /> Grammar + spelling
                </span>
                <span className="flex items-center gap-2">
                  <Zap className="w-4 h-4 text-brand-500" /> Auto-posts comments
                </span>
              </div>
            </div>

            {/* Submit form */}
            <div className="lg:col-span-5 fade-up">
              <form
                onSubmit={onSubmit}
                data-testid="analyze-form"
                className="border border-zinc-800 bg-zinc-950/80 backdrop-blur-xl p-8 space-y-6"
              >
                <div className="flex items-center justify-between">
                  <h2 className="font-display font-bold text-xl tracking-tight">
                    Start a review
                  </h2>
                  <span
                    className="font-mono-tech text-[10px] uppercase tracking-widest px-2 py-1 border border-brand-500 text-brand-500"
                    data-testid="guest-badge"
                  >
                    Guest: {cfg.guest_name || "Spellchecker"}
                  </span>
                </div>

                <div className="space-y-2">
                  <Label
                    htmlFor="frameio-url"
                    className="font-mono-tech text-xs uppercase tracking-widest text-zinc-500"
                  >
                    <Link2 className="inline w-3 h-3 mr-1" />
                    Frame.io URL
                  </Label>
                  <Input
                    id="frameio-url"
                    data-testid="frameio-url-input"
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="https://app.frame.io/player/…"
                    className="bg-black border-zinc-800 text-white focus:border-brand-500 focus:ring-1 focus:ring-brand-500 font-mono-tech text-sm h-11"
                  />
                </div>

                <div className="space-y-2">
                  <Label
                    htmlFor="share-password"
                    className="font-mono-tech text-xs uppercase tracking-widest text-zinc-500"
                  >
                    <Lock className="inline w-3 h-3 mr-1" />
                    Share password (only if protected)
                  </Label>
                  <Input
                    id="share-password"
                    data-testid="share-password-input"
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="Leave empty if the share has no password"
                    className="bg-black border-zinc-800 text-white focus:border-brand-500 focus:ring-1 focus:ring-brand-500 font-mono-tech text-sm h-11"
                  />
                </div>

                <div className="space-y-2">
                  <Label
                    htmlFor="video-file"
                    className="font-mono-tech text-xs uppercase tracking-widest text-zinc-500"
                  >
                    <Upload className="inline w-3 h-3 mr-1" />
                    Or upload video (optional)
                  </Label>
                  <Input
                    id="video-file"
                    data-testid="video-file-input"
                    type="file"
                    accept="video/*"
                    onChange={(e) => setVideoFile(e.target.files?.[0] || null)}
                    className="bg-black border-zinc-800 text-white file:bg-zinc-900 file:text-white file:border-0 file:mr-3 file:py-2 file:px-3 h-auto"
                  />
                </div>

                <div className="space-y-2">
                  <Label
                    htmlFor="transcript"
                    className="font-mono-tech text-xs uppercase tracking-widest text-zinc-500"
                  >
                    <FileText className="inline w-3 h-3 mr-1" />
                    Transcript / script (optional)
                  </Label>
                  <Textarea
                    id="transcript"
                    data-testid="transcript-input"
                    value={transcript}
                    onChange={(e) => setTranscript(e.target.value)}
                    rows={3}
                    placeholder="Paste the script if you have one — gives more accurate grammar feedback."
                    className="bg-black border-zinc-800 text-white focus:border-brand-500 focus:ring-1 focus:ring-brand-500 text-sm resize-none"
                  />
                </div>

                <div className="flex items-center justify-between border-t border-zinc-900 pt-4">
                  <div className="space-y-0.5">
                    <Label
                      htmlFor="auto-post"
                      className="text-sm text-white cursor-pointer"
                    >
                      Auto-post comments to Frame.io
                    </Label>
                    <p className="text-xs text-zinc-600">
                      Posts each issue back as a timestamped comment.
                    </p>
                  </div>
                  <Switch
                    id="auto-post"
                    data-testid="auto-post-switch"
                    checked={autoPost}
                    onCheckedChange={setAutoPost}
                  />
                </div>

                <Button
                  type="submit"
                  data-testid="analyze-submit-btn"
                  disabled={submitting}
                  className="w-full bg-brand-500 hover:bg-brand-400 text-zinc-950 border-0 h-12 font-mono-tech uppercase tracking-widest text-xs rounded-none"
                >
                  {submitting ? "Starting..." : (
                    <>
                      Analyze video <ArrowRight className="ml-2 w-4 h-4" />
                    </>
                  )}
                </Button>
              </form>
            </div>
          </div>
        </div>
      </section>

      {/* HOW IT WORKS */}
      <section className="max-w-7xl mx-auto px-6 py-24">
        <div className="grid lg:grid-cols-12 gap-8 mb-16">
          <div className="lg:col-span-4">
            <span className="font-mono-tech text-xs text-brand-500 uppercase tracking-widest">
              How it works
            </span>
            <h2 className="font-display font-black text-3xl lg:text-4xl tracking-tighter mt-3">
              Built for animators
              <br />
              who ship daily.
            </h2>
          </div>
          <div className="lg:col-span-8 grid sm:grid-cols-2 gap-px bg-zinc-800">
            <Feature
              testid="feature-ocr"
              icon={Eye}
              title="01 · Frame-by-frame OCR"
              desc="ffmpeg samples one frame every 2 seconds. Each frame is passed to Gemini 3 Flash for OCR — including stylized animation titles."
            />
            <Feature
              testid="feature-ai"
              icon={Sparkles}
              title="02 · Spelling & grammar AI"
              desc="Detects misspellings, agreement errors, missing punctuation and bad capitalization. Suggests fixes you can paste straight in."
            />
            <Feature
              testid="feature-timestamp"
              icon={Zap}
              title="03 · Timestamps that match"
              desc="Every issue is tagged with the exact second it appears in the video, ready to drop into Frame.io's player."
            />
            <Feature
              testid="feature-autopost"
              icon={Link2}
              title="04 · Auto-posts comments"
              desc="Each issue becomes a comment on the Frame.io asset at the correct timestamp — your animators see it in their dashboard."
            />
          </div>
        </div>
      </section>
    </main>
  );
}

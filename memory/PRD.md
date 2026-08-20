# Frame.io QA – Proof.io

## Original problem statement
Build a website where the user pastes a Frame.io link; the app reads the video,
detects spelling & grammar mistakes in the on-screen text, and posts comments
back on the Frame.io asset at the exact timestamps. (Animation studio QA tool.)

## Architecture
- **Backend**: FastAPI + Motor (MongoDB) + google-genai (direct Gemini 3 Flash)
  + EasyOCR (local text detection) + ffmpeg + httpx for Frame.io V2 API.
- **Frontend**: React + Tailwind + shadcn/ui, dark cinematic theme (Outfit/IBM
  Plex Sans/JetBrains Mono).
- **Pipeline** (rewritten 2026-08-11, batched 2026-08-20, see below): URL →
  resolve asset_id → download video → ffmpeg fixed-interval frame sampling
  (`FRAME_SAMPLE_INTERVAL`, default 1.5s) → local EasyOCR pass, batched
  across frames (`ocr_service.ocr_frames_batch`, `OCR_BATCH_SIZE`) so the
  detector runs as one GPU forward pass per batch instead of per frame →
  noise filtering (isolated single characters dropped; a reading seen in
  only one sample is dropped unless confidently read, since real captions
  hold for multiple samples) → consecutive matching frames merged into
  distinct text instances, preferring a frame that's both visually settled
  *and* textually unchanged from its neighbor as the representative (not
  mid-animation, not a still-incomplete typewriter reveal) → Gemini 3 Flash
  reads + spellchecks each instance's representative frame, batched across
  instances (`ai_service.analyze_frames_batch`, `GEMINI_BATCH_SIZE`) so
  request *count* — what the free-tier quota actually limits — stays well
  below one-per-instance → thumbnail cropped from detected text region →
  optional transcript pass → post each issue back to Frame.io with
  timestamp.
- **Gemini quota exhaustion is handled explicitly, not silently masked**: a
  429 raises `ai_service.GeminiQuotaExceeded` distinctly from "checked, found
  nothing" (previously both looked identical). The pipeline stops attempting
  further Gemini calls once quota is confirmed gone, but keeps running local-
  only checks (contrast) for every remaining instance, and records
  `quota_exceeded` / `unchecked_instances` on the analysis with a plain-
  language note in the completion message.
- **Investigated and reverted (2026-08-20)**: content-adaptive frame
  extraction via ffmpeg's `mpdecimate` filter, to replace fixed-interval
  sampling with "keep a frame only when the picture changes, but never skip
  more than N seconds." Real, sourced technique (same pattern used by
  VideOCR/videocr-PaddleOCR), but empirically it does not beat fixed-interval
  for this project's actual test content even at maximum tolerance (136
  frames vs. 139 baseline; default tuning produced 5,397) — this video has
  continuous whole-frame variation (background motion/compression grain)
  under otherwise-static text, which defeats whole-frame pixel diffing even
  when the on-screen text itself hasn't changed. A text-*region*-aware dedup
  (diff just the detected text area, not the whole frame) might still work
  where this didn't; not yet built.
- **Why the rewrite**: the old "one Gemini call per frame every 2s" design
  could silently miss any on-screen text visible for under 2 seconds
  (sampling-theorem gap), reported duplicate issues for text held across
  multiple samples, and paid for one Gemini image call per sample regardless
  of whether the frame had any text. Decoupling cheap local OCR (finds *where*
  text is) from Gemini (reads + judges spelling once per distinct instance)
  fixes all three at once and cuts Gemini call volume by ~90%+ on typical
  video.

## Integrations
- Gemini 3 Flash via a direct Google API key (`GEMINI_API_KEY`) — switched off
  the Emergent proxy key so per-call cost is transparent for pricing this as
  a paid product.
- Frame.io, two paths chosen per-analysis (rewritten 2026-08-19, see below):
  - **Connected (official V4 API, OAuth)**: Adobe IMS OAuth 2.0
    (`FRAMEIO_CLIENT_ID/SECRET/REDIRECT_URI`, `prompt=login` so an existing
    Adobe browser session can't silently auto-connect without the user
    seeing a login screen); real file download via the Files API, comments
    posted via the real Comments API with frame-accurate timestamps. A
    single Adobe identity can belong to several Frame.io accounts with no
    "default" indicated anywhere in the API — `FrameioSession.account_ids`
    stores all of them, and posting tries each until one can see the file
    (`_resolve_reachable_account_id`), rather than guessing the first one.
    Only works at all if the file is in a project one of those accounts is
    a member of — holding a public share link never grants that membership.
  - **Anonymous (guest, Playwright)**: headless Chromium opens the public
    share link, scrapes the signed video URL, posts comments by simulating
    keyboard seeks + typing as guest "Proof.io". The only way to handle
    a share link from someone else's account (no official share-lookup
    endpoint exists).
  - Every V4 request and response body is wrapped in a JSON:API-style
    `{"data": ...}` envelope — undocumented for responses (found via live
    testing), documented for request bodies once we knew to look
    (`services/frameio_api.py`).
- Product scope is spelling/grammar only — punctuation and capitalization are
  excluded both in the Gemini prompt and defensively filtered server-side
  (`SKIPPED_ISSUE_TYPES`).
- Optional contrast check (opt-in, 2026-08-19): WCAG-ratio check reusing the
  OCR bounding boxes already produced for the spelling pass — no new
  detection step, no extra Gemini calls (`ocr_service.check_contrast`).
  Approximates foreground/background color via the 5th/95th luminance
  percentile within each box and checks only the instance's one
  representative frame; known to false-positive on outlined/drop-shadowed
  captions and can miss a brief bad-contrast moment within a longer
  instance. Covers what was tracked as "colour contrast, text
  visibility/legibility" in the backlog.

## Endpoints
- `GET  /api/config`
- `POST /api/analyses` (multipart: frameio_url, transcript, auto_post, video)
- `GET  /api/analyses`
- `GET  /api/analyses/{id}`
- `POST /api/analyses/{id}/post` — bulk-post unposted issues
- `POST /api/analyses/{id}/issues/{issue_id}/post` — post one issue
- `DELETE /api/analyses/{id}` — soft delete (`deleted: true`), never erases
- `GET  /api/frameio/oauth/authorize` — redirects to Adobe IMS login
- `GET  /api/frameio/oauth/callback` — exchanges code, sets session cookie
- `GET  /api/frameio/oauth/status` — `{"connected": bool}`
- `POST /api/frameio/oauth/disconnect`

All three posting call sites (auto-post pipeline, bulk post, single-issue
post) route through one shared `_post_issues_to_frameio()` in `server.py`:
official API if the connected identity can reach the file, guest fallback
otherwise. Kept intentionally as one function after a bug where the two
manual-post endpoints had their own copy that never got updated to try the
official path at all.

## Status (2026-08-20)
- [x] Landing page with Frame.io URL input, optional video upload, transcript
- [x] Analysis page with live progress polling, thumbnail + time-range per
      issue, manual post button (bulk and per-issue)
- [x] History page (soft-deleted analyses hidden, not erased; auto-purged
      after `PURGE_AFTER_DAYS`, default 30, via a daily background task)
- [x] Frame.io asset id parsing + asset fetch + video download
- [x] Fixed-interval ffmpeg sampling + batched local OCR + noise filtering
      + settled/complete-frame selection → merged into distinct on-screen
      text instances, not per sampled frame (see Architecture)
- [x] Gemini 3 Flash spelling/grammar-only analysis (punctuation/
      capitalization explicitly out of scope), batched across instances
- [x] Gemini quota exhaustion surfaced explicitly, not silently masked;
      local-only checks (contrast) keep working past the quota wall
- [x] Optional transcript pass
- [x] Frame.io OAuth (Adobe IMS) connect/disconnect, tries every account the
      identity belongs to, official V4 API posting where reachable,
      guest-scrape fallback (posts as "Proof.io") otherwise
- [x] Opt-in WCAG contrast check per on-screen text instance
- [x] Optional brand/name/slang allowlist (landing page field): injected
      into the Gemini prompt and defensively post-filtered server-side
      (`ai_service.parse_allowlist`), same pattern as `SKIPPED_ISSUE_TYPES`
- [x] Inline video player on the Analysis page with click-to-seek per issue.
      `GET /api/analyses/{id}/video-url` resolves a fresh, directly-playable
      URL on demand (official API if reachable, guest-scrape otherwise) --
      never persisted or cached, consistent with the pipeline never storing
      the video itself. No player at all for uploaded-file analyses (no
      video survives past processing to resolve a URL for).

## Backlog / P1
- Delete button in the frontend (soft-delete + auto-purge both work
  end-to-end server-side; nothing in `History.jsx` actually calls
  `deleteAnalysis` yet)
- Annotation drawing on issues (Frame.io supports it)
- Slack / email digest of new reviews
- Multi-tenant + auth (currently single-user; one Frame.io session per
  browser cookie, no app-level accounts)
- Confidence scoring surfaced from Gemini's own judgment, not a hardcoded
  severity-by-type table
- Restore UI for soft-deleted analyses (purge/hide both work; no way to
  un-hide one from the frontend, Mongo-only)
- Text-region-aware duplicate-frame detection — see the reverted
  `mpdecimate` note in Architecture; diffing just the detected text area
  instead of the whole frame might survive the background-motion problem
  that killed the whole-frame approach, not yet attempted

## Done
- ~~Group near-duplicate issues across consecutive frames~~ — resolved
  structurally by the OCR-instance-merge rewrite (one issue-set per distinct
  text instance, not per sampled frame).
- ~~Colour contrast / text visibility check~~ — shipped as opt-in WCAG
  contrast check, see Integrations above.
- ~~Brand/name/slang allowlist~~ — shipped as an optional landing-page field.
- ~~Too many Gemini calls / quota exhaustion~~ — cut via noise filtering
  (fewer instances worth calling at all) and request batching (fewer calls
  per instance count); quota exhaustion itself now surfaced rather than
  silently mistaken for a clean pass.
- ~~OCR/analysis pipeline slow~~ — profiled with real per-stage timing logs;
  EasyOCR confirmed as the dominant cost (not a CPU-fallback regression —
  GPU verified active), addressed via batched detection.
- ~~Guest posting identity~~ — renamed from "Spellchecker" to "Proof.io"
  everywhere (backend constant, completion messages, frontend fallback).

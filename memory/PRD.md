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
- **Pipeline** (rewritten 2026-08-11, see below): URL → resolve asset_id →
  download video → ffmpeg dense frame sampling (0.5s) → local EasyOCR pass
  finds which frames have text → consecutive matching frames merged into
  distinct text instances (start/end timestamps, dedup for free) → Gemini 3
  Flash reads + spellchecks once per instance's clearest frame → thumbnail
  cropped from detected text region → optional transcript pass → post each
  issue back to Frame.io with timestamp.
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
    (`FRAMEIO_CLIENT_ID/SECRET/REDIRECT_URI`); real file download via the
    Files API, comments posted via the real Comments API with frame-accurate
    timestamps. Only works if the file is in a project this identity is a
    member of.
  - **Anonymous (guest, Playwright)**: headless Chromium opens the public
    share link, scrapes the signed video URL, posts comments by simulating
    keyboard seeks + typing as guest "Spellchecker". The only way to handle
    a share link from someone else's account (no official share-lookup
    endpoint exists).
  - Every V4 request and response body is wrapped in a JSON:API-style
    `{"data": ...}` envelope — undocumented for responses (found via live
    testing), documented for request bodies once we knew to look
    (`services/frameio_api.py`).
- Product scope is spelling/grammar only — punctuation and capitalization are
  excluded both in the Gemini prompt and defensively filtered server-side
  (`SKIPPED_ISSUE_TYPES`).

## Endpoints
- `GET  /api/config`
- `POST /api/analyses` (multipart: frameio_url, transcript, auto_post, video)
- `GET  /api/analyses`
- `GET  /api/analyses/{id}`
- `POST /api/analyses/{id}/post` — bulk-post unposted issues
- `POST /api/analyses/{id}/issues/{issue_id}/post` — post one issue
- `DELETE /api/analyses/{id}`
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

## Status (2026-08-19)
- [x] Landing page with Frame.io URL input, optional video upload, transcript
- [x] Analysis page with live progress polling, thumbnail + time-range per
      issue, manual post button (bulk and per-issue)
- [x] History page
- [x] Frame.io asset id parsing + asset fetch + video download
- [x] Dense ffmpeg sampling (0.5s) + local OCR merge → one Gemini call per
      distinct on-screen text instance, not per sampled frame
- [x] Gemini 3 Flash spelling/grammar-only analysis (punctuation/
      capitalization explicitly out of scope)
- [x] Optional transcript pass
- [x] Frame.io OAuth (Adobe IMS) connect/disconnect, official V4 API posting
      for files the connected account can reach, guest-scrape fallback
      otherwise

## Backlog / P1
- Inline video player on analysis page with seek-to-timestamp
- Annotation drawing on issues (Frame.io supports it)
- Slack / email digest of new reviews
- Multi-tenant + auth (currently single-user; one Frame.io session per
  browser cookie, no app-level accounts)
- Brand/name/slang allowlist to cut false positives
- Confidence scoring surfaced from Gemini's own judgment, not a hardcoded
  severity-by-type table
- Future scope (explicitly deferred, not started): colour contrast, text
  visibility/legibility checks

## Done
- ~~Group near-duplicate issues across consecutive frames~~ — resolved
  structurally by the OCR-instance-merge rewrite (one issue-set per distinct
  text instance, not per sampled frame).

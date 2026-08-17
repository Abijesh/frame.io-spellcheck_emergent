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
- Frame.io legacy V2 API with developer token (`FRAMEIO_TOKEN`).

## Endpoints
- `GET  /api/config`
- `POST /api/analyses` (multipart: frameio_url, transcript, auto_post, video)
- `GET  /api/analyses`
- `GET  /api/analyses/{id}`
- `POST /api/analyses/{id}/post`
- `DELETE /api/analyses/{id}`

## Status (2026-02-24)
- [x] Landing page with Frame.io URL input, optional video upload, transcript
- [x] Analysis page with live progress polling, issue list, manual post button
- [x] History page
- [x] Frame.io asset id parsing + asset fetch + video download
- [x] ffmpeg frame extraction every 2s
- [x] Gemini 3 Flash per-frame OCR + grammar analysis
- [x] Optional transcript pass
- [x] Auto post comments to Frame.io with timestamps

## Backlog / P1
- Inline video player on analysis page with seek-to-timestamp
- Frontend: display the new `thumbnail_b64` crop and `end_sec` range per
  issue (backend now produces both; UI doesn't show them yet)
- Annotation drawing on issues (Frame.io supports it)
- Slack / email digest of new reviews
- Multi-tenant + auth (currently single-user with shared Frame.io token)
- Brand/name/slang allowlist to cut false positives
- Confidence scoring surfaced from Gemini's own judgment, not a hardcoded
  severity-by-type table

## Done
- ~~Group near-duplicate issues across consecutive frames~~ — resolved
  structurally by the OCR-instance-merge rewrite (one issue-set per distinct
  text instance, not per sampled frame).

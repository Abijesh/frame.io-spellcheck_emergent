# Frame.io QA – Proof.io

## Original problem statement
Build a website where the user pastes a Frame.io link; the app reads the video,
detects spelling & grammar mistakes in the on-screen text, and posts comments
back on the Frame.io asset at the exact timestamps. (Animation studio QA tool.)

## Architecture
- **Backend**: FastAPI + Motor (MongoDB) + emergentintegrations (Gemini 3 Flash)
  + ffmpeg + httpx for Frame.io V2 API.
- **Frontend**: React + Tailwind + shadcn/ui, dark cinematic theme (Outfit/IBM
  Plex Sans/JetBrains Mono).
- **Pipeline**: URL → resolve asset_id → download video → ffmpeg frames every
  2s → Gemini 3 Flash OCR + spelling/grammar per frame → optional transcript
  pass → post each issue back to Frame.io with timestamp.

## Integrations
- Gemini 3 Flash via Emergent Universal Key (`EMERGENT_LLM_KEY`).
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
- Group near-duplicate issues across consecutive frames (right now we'll dedupe
  only after user feedback)
- Annotation drawing on issues (Frame.io supports it)
- Slack / email digest of new reviews
- Multi-tenant + auth (currently single-user with shared Frame.io token)

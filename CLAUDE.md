# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

CASAD Bridge is a WhatsApp-based inspection automation system for CASAD Consultants. Field inspectors send text, voice notes, and photos via WhatsApp; the system transcribes audio (Groq Whisper), parses structured data (Claude Haiku), and generates professional bridge inspection reports in three formats: Word (.docx), Excel R&B, and Excel AMC.

## Running the Project

**Development server:**
```bash
python server.py          # Flask dev server on port 5000
```

**Production:**
```bash
gunicorn -w 4 server.py
```

**Local testing without WhatsApp:**
```bash
python test_sim.py                    # Interactive bot simulator
python test_sim.py --auto             # Auto-run full scenario (Word)
python test_sim.py --auto --fmt 2     # Auto-run Excel R&B
python test_sim.py --auto --fmt 3     # Auto-run Excel AMC
python test_sim.py --reset            # Reset test database
```

**Fast report iteration (no WhatsApp needed):**
```bash
python run_report.py                  # Excel R&B from fixture JSON
python run_report.py --fmt word
python run_report.py --fmt excel_amc
python run_report.py --no-photos      # Skip photo processing (much faster)
python run_report.py --fixture my_fixture.json
```

**Tests:**
```bash
python -m pytest test_appendix_cells.py -v    # Excel cell regression suite
```

**Install dependencies:**
```bash
pip install -r requirements.txt
```

## Environment Variables

Copy from `.env.template`. Required:

| Variable | Purpose |
|---|---|
| `WHATSAPP_TOKEN` | Meta Cloud API temporary access token |
| `PHONE_NUMBER_ID` | WhatsApp Business phone number ID |
| `VERIFY_TOKEN` | Webhook verification token (default: `casad2024`) |
| `ANTHROPIC_API_KEY` | Claude API for AI parsing and defect coords |
| `GROQ_API_KEY` | Groq Whisper for audio transcription |
| `DATABASE_URL` | PostgreSQL connection string (Render sets this automatically) |
| `DASHBOARD_TOKEN` | Token for `/dashboard` and `/download` endpoints |

Optional (Google Drive upload):
- `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_DRIVE_FOLDER_ID`

Local path overrides: `MEDIA_DIR`, `EXCEL_TEMPLATE_PATH`, `AMC_TEMPLATE_PATH`, `OUTPUT_DIR`, `TEMPLATE_PATH`

## Architecture

The system is a **conversational state machine**. Each WhatsApp session progresses through states: `format_select` → `menu` → sections `1`–`5` → `confirm_generate` → `confirm_exit`.

### Module Map

| File | Role |
|---|---|
| `server.py` | Flask webhook server, session state machine, `/webhook` `/health` `/dashboard` `/download` |
| `db.py` | PostgreSQL session + message persistence; images stored as BYTEA; threaded connection pool |
| `whatsapp.py` | Meta Graph API v19.0 — parse payload, download media, send text/documents |
| `transcribe.py` | Groq Whisper v3 — OGG Opus audio → text (Hindi/Gujarati/English) |
| `ai_parse.py` | Core AI extraction — Claude Haiku parses unstructured field notes into structured JSON for each of the three report formats |
| `mark_image.py` | Claude Vision — locates defect center in a photo; returns normalized (x%, y%) 0–1 coords |
| `report_gen.py` | Word report builder — fills `casad_template.docx`, adds photos with hyperlinks |
| `report_gen_excel.py` | Excel R&B builder — fills `casad_excel_template.xlsx` (Appendix-A + Appendix-B defect matrices) |
| `report_gen_excel_amc.py` | Excel AMC builder — fills `casad_amc_template.xlsx` (AMC format, multi-sheet) |
| `drive.py` | Optional Google Drive upload via service account |

### Data Flow

```
WhatsApp → /webhook → categorize (1–5) → store to PostgreSQL
                                ↓
                     Collecting: text stored, audio transcribed, images compressed+saved
                                ↓ (user triggers generate)
                     ai_parse.py [Claude Haiku] → structured JSON
                     mark_image.py [Claude Vision] → defect coordinates (rate-limited)
                     report_gen*.py → .docx / .xlsx
                     send_document() → WhatsApp
```

### Key Implementation Notes

- **Claude rate limiting**: `mark_image.py` sleeps 13s between photos (5 req/min Haiku limit). 80 damage photos ≈ 17 min processing.
- **JSON repair**: Claude output is parsed with 3-step fallback — direct parse → `json_repair` library → manual bracket closing.
- **WhatsApp deduplication**: `processed_ids` set in `server.py` prevents duplicate handling of Meta API retries.
- **Report generation is async**: `server.py` uses `ThreadPoolExecutor` so webhook can return 200 immediately.
- **DB images as BYTEA**: Images survive server restarts on Render (no ephemeral filesystem dependency).

### Template Files

These are binary files checked into the repo. Re-generate with:
- `python create_template.py` → `casad_template.docx`
- `python generate_cost_sheet.py` → cost analysis sheet

Test fixture: `test_fixture_khokhara.json` — a complete sample inspection (Khokhara ROB).

"""
Document Processing Automation — Steps 3-5
----------------------------------------------
Reads PDFs from a folder, extracts raw text, sends that text to Gemini
to pull out structured fields, and writes the results to a CSV file.

Works on any PDF (invoice, receipt, email printed/saved as PDF, etc.) —
it doesn't assume a specific layout, since the extraction step uses an
LLM rather than fixed regex/positions.

Uses Google's Gemini API, which has a genuine free tier (no card
required) — a good fit for prototyping this before paying for
anything. Swapping to Claude or another provider later only means
changing the extract_fields() function below; the read/store logic
stays the same.

Setup
-----
    pip install google-genai pdfplumber

Get a free API key (no billing required):
    1. Go to https://aistudio.google.com
    2. Sign in with a Google account
    3. Click "Get API key" -> "Create API key"

    export GEMINI_API_KEY="your-key-here"   # macOS/Linux
    setx GEMINI_API_KEY "your-key-here"      # Windows (new terminal after)

Usage
-----
    Run once, processing only files not already processed before:
        python extract_pipeline.py /path/to/pdf_folder

    Run once, reprocessing everything from scratch:
        python extract_pipeline.py /path/to/pdf_folder --reset

    Run continuously, checking the folder every N minutes (default 10)
    for new PDFs — this is the automated trigger (step 7):
        python extract_pipeline.py /path/to/pdf_folder --watch
        python extract_pipeline.py /path/to/pdf_folder --watch 30

Output
------
    output/results.csv        -> successfully extracted rows (all runs combined)
    output/needs_review.csv   -> files that failed to parse, with the reason
    output/processed.json     -> log of filenames already handled, so a
                                  re-run or scheduled check skips them
"""

import sys
import csv
import json
import time
import pdfplumber
import schedule
from pathlib import Path
from datetime import datetime
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Tried in order. If the first is overloaded (503), the next is used.
# All are free on the no-billing tier.
MODELS = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]

MAX_RETRIES = 3       # per model, before moving to the next one
RETRY_DELAY_SECONDS = 5

# The fields we want pulled out of every document. Edit this to match
# what you actually need (e.g. add "invoice_number", "due_date", etc.)
FIELDS = ["date", "sender", "amount", "description"]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "description": "Document date in YYYY-MM-DD format if found, else empty string.",
        },
        "sender": {
            "type": "string",
            "description": "Who sent or issued the document (company or person name).",
        },
        "amount": {
            "type": "string",
            "description": "The total monetary amount on the document, including currency symbol if present. Empty string if none.",
        },
        "description": {
            "type": "string",
            "description": "One short sentence summarizing what the document is for.",
        },
    },
    "required": FIELDS,
}


# ---------------------------------------------------------------------------
# Step 3: Read — extract raw text from a PDF
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path: Path) -> str:
    """Pull all text out of a PDF using pdfplumber."""
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n".join(text_parts)


# ---------------------------------------------------------------------------
# Step 4: Extract — send raw text to Gemini, get structured JSON back
# ---------------------------------------------------------------------------

def extract_fields_with_gemini(client: genai.Client, raw_text: str) -> dict:
    """
    Send document text to Gemini and force a structured JSON response by
    passing response_schema. This is more reliable than asking for "JSON
    only" in the prompt, since the model is constrained to the schema.

    Retries on 503 (model overloaded — common on the free tier during
    high-traffic periods) with a short delay, and falls back through
    MODELS in order if a model keeps failing.
    """
    if not raw_text.strip():
        raise ValueError("No extractable text in PDF (likely a scanned image — needs OCR).")

    prompt = (
        "Extract the requested fields from this document text. "
        "If a field isn't present, return an empty string for it.\n\n"
        f"--- DOCUMENT TEXT ---\n{raw_text[:8000]}"
    )
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
    )

    last_error = None
    for model in MODELS:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                if not response.text:
                    raise ValueError("Gemini did not return any structured output.")
                return json.loads(response.text)
            except (genai_errors.ServerError, genai_errors.ClientError) as e:
                # 503 = model overloaded, 429 = rate limit hit — both are
                # worth a short wait and retry, or moving to the next model.
                code = getattr(e, "code", None)
                if code not in (429, 503):
                    raise  # a real error (bad request, auth, etc.) — don't retry
                last_error = e
                reason = "rate-limited" if code == 429 else "overloaded"
                print(f"   ({model} {reason}, attempt {attempt}/{MAX_RETRIES})")
                time.sleep(RETRY_DELAY_SECONDS)
        print(f"   ({model} still unavailable after {MAX_RETRIES} tries, trying next model)")

    raise last_error or ValueError("All models failed with no error captured.")


# ---------------------------------------------------------------------------
# Step 5: Store — write results to CSV (success + needs-review)
# ---------------------------------------------------------------------------

def write_csv(rows: list[dict], path: Path, fieldnames: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def reset_output_files(*paths: Path):
    """Delete previous output files — used only with --reset, for a
    full from-scratch run."""
    for path in paths:
        if path.exists():
            path.unlink()


def load_processed_log(log_path: Path) -> set:
    """Filenames already handled (success or failure) in past runs,
    so a re-run or a scheduled check only processes what's new."""
    if not log_path.exists():
        return set()
    return set(json.loads(log_path.read_text(encoding="utf-8")))


def save_processed_log(log_path: Path, processed: set):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(sorted(processed)), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(pdf_folder: str, reset: bool = False):
    pdf_folder = Path(pdf_folder)
    output_dir = Path("output")
    results_csv = output_dir / "results.csv"
    review_csv = output_dir / "needs_review.csv"
    log_path = output_dir / "processed.json"

    if reset:
        reset_output_files(results_csv, review_csv, log_path)

    processed = load_processed_log(log_path)

    all_pdfs = sorted(pdf_folder.glob("*.pdf"))
    pdf_files = [p for p in all_pdfs if p.name not in processed]

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not pdf_files:
        print(f"[{timestamp}] No new PDFs to process ({len(all_pdfs)} total, all already done).")
        return

    client = genai.Client()  # reads GEMINI_API_KEY from the environment

    print(f"[{timestamp}] Found {len(pdf_files)} new PDF(s) out of {len(all_pdfs)} total. Processing...\n")

    for pdf_path in pdf_files:
        print(f"-> {pdf_path.name}")
        try:
            raw_text = extract_text_from_pdf(pdf_path)
            fields = extract_fields_with_gemini(client, raw_text)
            row = {"filename": pdf_path.name, **fields}
            write_csv([row], results_csv, fieldnames=["filename", *FIELDS])
            print("   extracted OK")
        except Exception as e:
            # Never let one bad document crash the whole batch —
            # log it and move on.
            review_row = {"filename": pdf_path.name, "error": str(e)}
            write_csv([review_row], review_csv, fieldnames=["filename", "error"])
            print(f"   FAILED — logged to needs_review.csv ({e})")

        # Mark as handled either way, so a failed file isn't retried
        # forever on every scheduled check — it stays visible in
        # needs_review.csv for you to deal with manually.
        processed.add(pdf_path.name)
        save_processed_log(log_path, processed)

    print(f"\nDone. Results: {results_csv}  |  Needs review: {review_csv}")


def watch_folder(pdf_folder: str, interval_minutes: int):
    """Automated trigger (step 7): re-check the folder on a schedule
    and process any new PDFs that have shown up since the last check."""
    print(f"Watching '{pdf_folder}' every {interval_minutes} minute(s). Press Ctrl+C to stop.\n")
    run_pipeline(pdf_folder)  # run once immediately, then on schedule
    schedule.every(interval_minutes).minutes.do(run_pipeline, pdf_folder=pdf_folder)
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("Usage: python extract_pipeline.py /path/to/pdf_folder [--reset | --watch [minutes]]")
        sys.exit(1)

    folder = args[0]

    if "--watch" in args:
        idx = args.index("--watch")
        minutes = int(args[idx + 1]) if len(args) > idx + 1 and args[idx + 1].isdigit() else 10
        watch_folder(folder, minutes)
    elif "--reset" in args:
        run_pipeline(folder, reset=True)
    else:
        run_pipeline(folder)

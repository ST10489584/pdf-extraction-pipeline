# Document Processing Automation

Extracts structured data from unstructured PDFs (invoices, receipts, emails
saved as PDF) using Google's Gemini API, and logs the results to a CSV file
— no manual data entry needed.

## How it works

1. **Read** — every PDF in a folder has its raw text pulled out with `pdfplumber`.
2. **Extract** — the raw text is sent to Gemini, which is forced (via a JSON
   response schema) to return structured fields: `date`, `sender`, `amount`,
   `description`.
3. **Store** — results are appended to `output/results.csv`. Any document that
   fails to parse (e.g. a scanned image with no extractable text) is logged
   to `output/needs_review.csv` with the reason, instead of crashing the run.

## Setup

```bash
pip install -r requirements.txt
```

Get a **free** API key (no billing required):
1. Go to [aistudio.google.com](https://aistudio.google.com)
2. Sign in with a Google account
3. Click **Get API key** → **Create API key**

```bash
export GEMINI_API_KEY="your-key-here"
```

## Usage

```bash
python extract_pipeline.py /path/to/pdf_folder
```

## Example

| filename        | date       | sender      | amount  | description                  |
|------------------|------------|-------------|---------|-------------------------------|
| invoice_014.pdf  | 2026-09-12 | Acme Supply | R1,240  | Office stationery order       |

## Next steps (roadmap)

- [ ] Swap CSV for Google Sheets (`gspread`) or SQLite
- [ ] Trigger automatically on new files (`schedule` library, cron, or IMAP watch)
- [ ] Test against 5–10 real sample documents and refine the extraction prompt
- [ ] Record a before/after demo for the portfolio
"# pdf-extraction-pipeline" 

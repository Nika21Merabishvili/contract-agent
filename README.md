# nxia-contract-agent

Analyses a service contract (PDF) against Article 104 of the Tax Code of
Georgia using a local Qwen model via Ollama, and outputs structured JSON.
[export_excel.py](export_excel.py) then turns that JSON into an Excel workbook.

## Requirements

- [Ollama](https://ollama.com) **0.5.0 or newer** (structured outputs), with the
  model pulled: `ollama pull qwen3.5:4b`
- Python packages: `pip install -r requirements.txt` (pinned; authoritative).
  `pdfplumber` is optional but recommended — without it, tables reach the model
  as interleaved columns and figures inside them get lost. `openpyxl` is needed
  only for step 2, the Excel export; `flask` (plus `flask-wtf`, `bcrypt`,
  `waitress`) only for the web app; `pytesseract` + `pypdfium2` only for scanned
  PDFs (see below). The CLI needs none of the web/security packages.
- **For scanned (image-only) PDFs only:** the
  [Tesseract OCR engine](https://github.com/tesseract-ocr/tesseract) — a
  **system binary, not a pip package** — with **both** the English and Georgian
  language packs (`eng`, `kat`). See [Scanned PDFs](#scanned-pdfs-ocr-fallback).
  Everything else works without it.
- The Georgian text of Article 104 in [knowledge/](knowledge/) — see
  [knowledge/README.md](knowledge/README.md).

## Usage

```
python pdf_analyze.py                     # prompts for a contract PDF
python pdf_analyze.py contract.pdf
python pdf_analyze.py contract.pdf --out result.json
python pdf_analyze.py contract.pdf --article-lang ka   # reason over the Georgian statute
python pdf_analyze.py --dump-article104   # verify Article 104 loads as readable text
python pdf_analyze.py contract.pdf --show-input   # debug: see every prompt sent

python pdf_analyze.py a.pdf b.pdf c.pdf    # batch: one JSON array, one contract at a time
python pdf_analyze.py a.pdf b.pdf c.pdf --xlsx   # batch straight to one workbook
```

Give it more than one PDF and it runs each one through the same four-call
pipeline in its own turn (see [How it works](#how-it-works)) — never several
contracts in one prompt — then prints a JSON array instead of one object. A
contract that fails (no extractable text, or no tax verdict reached) is skipped
with a warning on stderr; the rest still come back. `--dump-text` needs exactly
one PDF, since dumping text for a batch would be ambiguous.

The result is printed to stdout with three blocks (a single object for one PDF,
an array of these for a batch):

- `contract_data` — 23 fields (parties, service type, value, dates, …),
  values in Georgian, proper nouns kept as written in the contract, missing
  values as `არ არის მითითებული`.
- `tax_analysis` — 4 fields: Georgian-source income yes/no with a clause-level
  justification from Article 104, and the withholding / reverse-charge VAT
  conclusion with reasoning.
- `_audit` — the full Article 104 clause checklist and the clauses cited, so the
  reasoning can be scored rather than just the verdict.

## Scanned PDFs (OCR fallback)

A photographed or scanned contract has no embedded text layer, so `pdfplumber`
and `pypdf` extract nothing from it. When that happens — and **only** then —
the extractor falls back to OCR: each page is rendered to a ~300 DPI grayscale
image with `pypdfium2` and read by [Tesseract](https://github.com/tesseract-ocr/tesseract)
via `pytesseract`, with **both English and Georgian** language data loaded
(contracts here are either, or mixed — Georgian OCR'd with only `eng` loaded
comes back as gibberish). The recovered text then flows through the exact same
four-call pipeline; nothing downstream changes. A PDF with a real text layer is
never OCR'd — the embedded text is always more accurate.

Toolchain tradeoff: `pypdfium2` + `pytesseract` was chosen over running
[OCRmyPDF](https://ocrmypdf.readthedocs.io) on the file because it needs no
system dependencies beyond Tesseract itself (OCRmyPDF pulls in Ghostscript and
friends). The cost is that the OCR'd text lives only in memory for the run —
the PDF on disk is not rewritten with a text layer.

Installing Tesseract (a system binary plus language packs, **not** pip):

- **Windows:** `winget install UB-Mannheim.TesseractOCR`, then download
  [`kat.traineddata`](https://github.com/tesseract-ocr/tessdata) and copy it
  into `C:\Program Files\Tesseract-OCR\tessdata` (needs admin). Without admin
  rights, put `eng.traineddata` and `kat.traineddata` in
  `%LOCALAPPDATA%\nxia-contract-agent\tessdata` instead — the app finds them
  there automatically.
- **Debian/Ubuntu:** `sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-kat`
- **macOS:** `brew install tesseract tesseract-lang`

Behaviour worth knowing:

- **OCR output is flagged for verification.** OCR can misread exactly the
  characters this tool exists to copy verbatim — names, tax IDs, amounts,
  dates. An OCR'd contract's JSON carries `"_source": "ocr"`, the web page
  lists which files were OCR-read, and the workbook gains a trailing
  "შენიშვნა" column telling the reader to verify those fields. None of that
  appears for normal text-layer PDFs.
- **Unreadable stays unreadable.** If OCR yields nothing plausible (a blank
  scan, a photo too poor to read), the file fails with a clear message rather
  than feeding garbage into the analysis — same principle as a tax verdict the
  model couldn't reach: an honest failure beats a wrong-but-confident row.
- **Missing Tesseract is a message, not a crash** — it names what to install,
  including the Georgian pack, and only comes up when a scanned PDF actually
  arrives.
- OCR adds noticeable time (seconds per page, before the model even starts).
- `--no-ocr` skips the fallback for debugging; scanned PDFs then fail as
  unreadable, as they did before OCR existed.

## Step 2 — the Excel workbook

`pdf_analyze.py` is "PDF → JSON"; [export_excel.py](export_excel.py) is
"JSON → xlsx". They are separate programs, so a saved analysis can be
re-exported without paying for the model again:

```
python pdf_analyze.py contract.pdf | python export_excel.py -o out.xlsx
python pdf_analyze.py contract.pdf > result.json
python export_excel.py result.json                # reads a file; -o is optional
python pdf_analyze.py contract.pdf --xlsx         # both steps in one go
```

It reads a JSON file or piped stdin, and accepts either a single analysis object
or a list of them — so several analyses become several rows in one sheet.

One workbook, one sheet, one table: row 1 is the Georgian header (one column per
field, `contract_data` then `tax_analysis`), and each analysis below it is one
row, values copied verbatim. Headers come from `FIELD_LABELS` in
[georgian.py](georgian.py) with the rest of the app's Georgian, not from a
second list that could drift. A field missing from the JSON gets the same
`არ არის მითითებული` the pipeline itself uses, so a partially filled contract
still yields a complete, aligned row. `_audit` never reaches the sheet — it is a
reasoning trace for scoring the model, not something an end user reads.

A batch of more than one contract (CLI `--xlsx`, or the web app) adds one
leading "წყარო ფაილი" (source file) column so each row stays identifiable —
`build_workbook(records, sources=[...])`. A single contract gets the plain sheet
with no extra column, exactly as it always has.

## Web app — upload contracts, get one Excel back

For a non-technical user, [app.py](app.py) puts a one-page browser UI in front of
the exact same pipeline — no terminal needed. It requires signing in (see
[SECURITY.md](SECURITY.md)):

```
python manage_users.py add <username>   # create a login (prompts for a password)
python app.py                           # DEVELOPMENT server on http://127.0.0.1:5000
python serve.py                         # PRODUCTION server (waitress) — deploy behind a TLS proxy
```

`python app.py` is Flask's development server — for local use only. The
network-reachable intranet deployment runs `serve.py` (waitress) behind a
TLS-terminating reverse proxy, with a session secret and per-user logins; the
full setup and the InfoSec assessment are in [SECURITY.md](SECURITY.md).

After signing in, choose one PDF or several —
the file picker takes any number. Clicking the button analyses each contract in
its own turn (exactly the CLI's batch behaviour above) and downloads one
`.xlsx`: a single contract keeps the plain sheet and its own filename; several
become one workbook (one row each, in upload order, with a source-file column)
named `contracts_<n>_<date>.xlsx`. The page shows "Analysing…" (or, for a
batch, "Analysing N contracts…") while the local model works — a scanned PDF
takes longer, since it is OCR'd first — then reports the result: which file(s),
if any, could not be analysed (unreadable even after OCR, or no tax verdict
reached) so a single bad PDF doesn't cost the rest of the batch, and which
file(s) were read via OCR and so warrant a check of names, ID numbers, amounts
and dates against the original.

It is a thin wrapper: the analysis is `pipeline.analyze_many` (looping
`pipeline.analyze`, the same single-contract call the CLI makes) and the
workbook is `export_excel.build_workbook`, the same code the CLI calls. Analysis
stays local — it talks only to the local Ollama, one request at a time, no job
queue, no cloud, no API keys, no database, and the contract text never leaves the
machine. The web tier around it was hardened for a network-reachable intranet
deployment (login, CSRF, security headers, upload validation, audit logging); see
[SECURITY.md](SECURITY.md). The terminal workflow above is unchanged.

## Running on a Modal GPU

By default everything runs against a **local** Ollama (`127.0.0.1:11434`), i.e.
this machine's CPU/GPU. The analysis model (`qwen3.6:35b`, ~23 GB) is slow on
CPU, so [modal_ollama.py](modal_ollama.py) can instead run Ollama on a rented GPU
([Modal](https://modal.com)) and expose it over HTTP. The app doesn't change —
only which host it points at, via the `OLLAMA_HOST` environment variable.

**No endpoint is hardcoded in this repo — deploy your own under your own Modal
account:**

```
pip install modal
modal token new                    # authenticate as yourself (one time)
modal deploy modal_ollama.py       # prints https://<your-workspace>--nxia-ollama-serve.modal.run
```

Then point the pipeline at that URL for the current session:

```
# Windows (PowerShell)
$env:MODAL_OLLAMA_HOST = "https://<your-workspace>--nxia-ollama-serve.modal.run"
.\run_modal.ps1                    # sets OLLAMA_HOST from it, then runs app.py

# or set OLLAMA_HOST directly for serve.py / the CLI
$env:OLLAMA_HOST = "https://<your-workspace>--nxia-ollama-serve.modal.run"
python serve.py
```

The container scales to zero when idle, so you are billed only for GPU seconds
while a request is in flight (or a warm container is waiting out its idle
window) — **on your Modal account.** If you don't want a cloud GPU at all, leave
`OLLAMA_HOST` unset and run a local Ollama with the models pulled.

## How it works

One call asking a 4B model for 27 fields plus cross-lingual statutory reasoning
returned nine empty fields. The work is now split four ways, so the model does one
job at a time:

| Call | Input | Output |
|---|---|---|
| 1. parties | contract text | 12 party fields |
| 2. terms | contract text | 12 commercial fields |
| 3. tax | facts from 1+2, plus Article 104 | forced clause-by-clause checklist, then a verdict |
| 4. translate | the 5 free-text fields | those fields in Georgian |

Python then assembles the JSON.

**The model does not write Georgian.** It emits English codes from closed sets
(`provider`, `non_resident`, `llc`, `milestone`, …) and [georgian.py](georgian.py)
maps them to Georgian. That covers 22 of the 27 fields, including every citation,
so those values cannot contain a spelling error — a human wrote them. Only five
genuinely free-text fields are translated by the model, in their own call.

The tax call never sees the contract, only the extracted facts. Giving it the
contract is what let it reach for the cargo-transport clause: the words were in
front of it.

### Constraints worth knowing before you change things

Ollama compiles the `format` schema into a GBNF grammar, and its converter does
**not** support all of JSON Schema. Probed against Ollama 0.32.0 / qwen3.5:4b:

| Keyword | Result |
|---|---|
| `enum`, `minLength`, `additionalProperties: false`, `type: integer` | enforced |
| `pattern` | **HTTP 400 — failed to parse grammar** |
| `anyOf` | **HTTP 400 — failed to parse grammar** |

So a date regex in the schema would not be ignored, it would break every run.
Dates are requested as ISO 8601 and validated/reformatted in Python instead.

`minLength` is set to 1 and never higher. A high floor does not buy reasoning:
given a stub answer and `minLength: 80`, the model pads to exactly 80 characters
with `"no\nto\nthe\nend.\n\nI\nhave\nnothing\nmore\nto\nsay. ..."`. Length floors
that matter are enforced in Python, where failing means retrying.

Sampling is pinned in `SAMPLING`. Two of those defaults are traps: this model
ships with `temperature 1.0` **and `presence_penalty 1.5`** (`ollama show`).
Penalties apply to logits before sampling, so `presence_penalty` skews output even
at temperature 0 — on a task that copies names and addresses verbatim, that is
pure harm.

### A missing fact is not a failed analysis

A contract that is silent on a fact yields `არ არის მითითებული`. A tax verdict the
model could not reach raises instead — it is never filled in. A conclusion the
model failed to reach must not land in the Excel looking like an extracted fact.

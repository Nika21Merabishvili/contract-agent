# nxia-contract-agent

Analyses a service contract (PDF) against Article 104 of the Tax Code of
Georgia using a local Qwen model via Ollama, and outputs structured JSON.
[export_excel.py](export_excel.py) then turns that JSON into an Excel workbook.

## Requirements

- [Ollama](https://ollama.com) **0.5.0 or newer** (structured outputs), with the
  model pulled: `ollama pull qwen3.5:4b`
- Python packages: `pip install -r requirements.txt`
  (or `pip install ollama pypdf pdfplumber openpyxl flask`). `pdfplumber` is
  optional but recommended — without it, tables reach the model as interleaved
  columns and figures inside them get lost. `openpyxl` is needed only for step 2,
  the Excel export; `flask` only for the web app.
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
```

The result is printed to stdout with three blocks:

- `contract_data` — 23 fields (parties, service type, value, dates, …),
  values in Georgian, proper nouns kept as written in the contract, missing
  values as `არ არის მითითებული`.
- `tax_analysis` — 4 fields: Georgian-source income yes/no with a clause-level
  justification from Article 104, and the withholding / reverse-charge VAT
  conclusion with reasoning.
- `_audit` — the full Article 104 clause checklist and the clauses cited, so the
  reasoning can be scored rather than just the verdict.

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

## Web app — upload a PDF, get the Excel back

For a non-technical user, [app.py](app.py) puts a one-page browser UI in front of
the exact same pipeline — no terminal needed:

```
python app.py            # starts a local server and prints the URL
```

Open the printed URL (http://127.0.0.1:5000), choose a contract PDF, and click the
button. It runs steps 1 and 2 and downloads the same `.xlsx` the CLI produces. The
page shows an "Analysing…" state while the local model works (this takes a minute),
and a plain-language message if the PDF has no extractable text (scanned image) or
the model cannot reach a tax verdict.

It is a thin wrapper: the analysis is `pipeline.analyze` and the workbook is
`export_excel.build_workbook`, the same code the CLI calls. It runs locally and
single-user — bound to `127.0.0.1`, talking only to the local Ollama, one request
at a time. No cloud, no API keys, no database. The terminal workflow above is
unchanged.

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

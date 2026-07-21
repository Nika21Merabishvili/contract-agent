"""Analyse a service contract against Article 104 of the Georgian Tax Code.

Entry point. Extracts text from a contract PDF and works through four small
model calls instead of one large one:

    1. parties   -- 12 fields, from the contract text
    2. terms     -- 12 fields, from the contract text
    3. tax       -- a forced clause-by-clause checklist over Article 104
    4. translate -- the five genuinely free-text fields, English -> Georgian

The model emits English codes from closed sets; `georgian.py` maps them to
Georgian. That keeps Georgian generation -- which a 4B model does badly -- out of
22 of the 27 output fields, and out of every citation.

Prints ONE JSON object (`contract_data`, `tax_analysis`, `_audit`) to stdout and
nothing else -- every diagnostic goes to stderr, so `... > out.json` is clean
JSON. `export_excel.py` turns that JSON into an Excel workbook; this program
never formats output itself. Saving to a file is currently disabled; uncomment
the marked lines at the end of cli.main() to re-enable it (then --out chooses the
destination).

The implementation is split across small, single-responsibility modules:

    extraction.py     -- PDF/knowledge file -> clean text (+ tables)
    ollama_client.py  -- the shared "prompt + schema -> validated JSON" call
    schemas.py        -- the JSON schema (and output field lists) for each call
    prompts.py        -- the prompt template for each call
    validation.py     -- date/reformatting and "is this a real answer" checks
    pipeline.py       -- the four-call sequence and final JSON assembly
    cli.py            -- argument parsing and main()
    georgian.py       -- English-code -> Georgian lookup tables (and field labels)
    diagnostics.py    -- where progress/telemetry go (stderr; --verbose for detail)
    errors.py         -- the shared exception hierarchy
    export_excel.py   -- step 2: that JSON -> a one-sheet Excel workbook

Usage:
    python pdf_analyze.py                          # prompts for a contract PDF
    python pdf_analyze.py contract.pdf
    python pdf_analyze.py contract.pdf | python export_excel.py -o out.xlsx
    python pdf_analyze.py contract.pdf --xlsx      # same, in one command
    python pdf_analyze.py contract.pdf --article-lang ka   # reason over the Georgian statute
    python pdf_analyze.py contract.pdf --verbose           # stream model output to stderr
    python pdf_analyze.py --dump-article104        # verify Article 104 loads cleanly
"""

from __future__ import annotations

from cli import main

if __name__ == "__main__":
    main()

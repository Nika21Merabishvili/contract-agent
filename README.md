# nxia-contract-agent

Analyses a service contract (PDF) against Article 104 of the Tax Code of
Georgia using a local Qwen model via Ollama, and outputs structured JSON.
A separate script (next step of the project) turns that JSON into an Excel
workbook.

## Requirements

- [Ollama](https://ollama.com) **0.5.0 or newer** (structured outputs), with the
  model pulled: `ollama pull qwen3.5:4b`
- Python packages: `pip install ollama pypdf`
- The Georgian text of Article 104 placed in [knowledge/](knowledge/) — see
  [knowledge/README.md](knowledge/README.md).

## Usage

```
python pdf_analyze.py                     # prompts for a contract PDF
python pdf_analyze.py contract.pdf        # writes contract.json next to the PDF
python pdf_analyze.py contract.pdf --out result.json
python pdf_analyze.py --dump-article104   # verify Article 104 loads as readable Georgian
python pdf_analyze.py contract.pdf --show-input   # debug: see the exact prompt sent
```

The result is printed to stdout and saved as a `.json` file with two blocks:

- `contract_data` — 23 fields (parties, service type, value, dates, …),
  values in Georgian, proper nouns kept as written in the contract, missing
  values as `არ არის მითითებული`.
- `tax_analysis` — 4 fields: Georgian-source income yes/no with a clause-level
  justification from Article 104, and the withholding / reverse-charge VAT
  conclusion with reasoning.

Output is schema-enforced: the JSON keys are fixed English identifiers, so the
downstream Excel builder can rely on them.

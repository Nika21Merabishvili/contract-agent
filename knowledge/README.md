# Knowledge base

Put the Georgian text of Article 104 of the Tax Code of Georgia in this folder.
`pdf_analyze.py` looks here automatically, preferring these names in order:

1. `article_104.txt` — **preferred.** Plain text, saved as UTF-8.
2. `article_104.md`
3. `article_104.pdf` — works, but PDF text extraction of Georgian script is
   unreliable in some files (broken font maps produce garbage the model cannot
   read).

After adding the file, always verify it loads as readable Georgian:

```
python pdf_analyze.py --dump-article104
```

If the output is boxes, Latin gibberish, or empty, open the PDF in a viewer,
copy the article text, and save it as `article_104.txt` (UTF-8) here instead.

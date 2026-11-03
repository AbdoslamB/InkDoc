# Benchmarks

The documents InkDoc's accuracy is measured on, the engine replies recorded for them, and the
locked output InkDoc's own code must keep producing. Development only: nothing here ships.

```
benchmarks/
├── corpus/            documents, one folder per kind of hard case
│   ├── 01_tables_single/   ruled, borderless, multi-line cells, merged headers
│   ├── 02_tables_multipage/
│   ├── 03_multi_column/
│   ├── 04_math/
│   ├── 05_charts_figures/
│   ├── 06_mixed_scans/
│   └── 07_broken_fonts/
├── recordings/        one JSON per engine reply, in a folder per document (by SHA-256)
├── baselines/         each case's Markdown, and locks.json with its hash and report
└── local/             the same layout for private documents (gitignored)
```

## What may be committed

Only documents anyone may redistribute: synthetic pages (`tests/_table_fixtures.py`), US
government works (public domain, for example SEC filings and agency reports), and papers under
CC-BY or CC0 (arXiv lists each paper's licence; "arXiv's non-exclusive licence" is **not**
enough). Next to each document, `NAME.source.json` says where it came from and under which
licence. Anything else, including any document a user sent, goes in `local/`.

A document is cut to the 1 to 3 pages that show one problem, so the whole set runs in seconds.

## Truth

Next to a document, the truth a person wrote, when there is one:

- `NAME.tables.json`: its tables (the format of `scripts/table_bench.py`);
- `NAME.expected.md`: its Markdown, for pages whose text layer can't be trusted (scans, broken
  fonts, an earlier OCR layer).

Without a truth file, numbers are judged against the PDF's own text layer
(`scripts/numeric_recall.py`), and every such score says "text-layer reference".

## Commands

```bash
# Run the real engines once (they must be installed), and record their replies.
python scripts/bench_replay.py record [--root benchmarks/local] [--levels fast,balanced,thorough] [--fill]

# Replay Auto over the recordings: every case must be the same as its lock.
python scripts/bench_replay.py replay [--root benchmarks/local] [--update-locks]

# What InkDoc does with one document, numbers included.
python scripts/pdf_diagnose.py benchmarks/corpus/01_tables_single/NAME.pdf

# Tables and numbers, per engine, before and after the table stage.
python scripts/table_bench.py benchmarks/corpus/01_tables_single
```

`replay` exits 1 on any change, miss or error. A miss means InkDoc asked an engine something
no recording answers (a router change that moves page boundaries, a new option): record again
with `--fill`. `--update-locks` locks the changed and new cases; review the diff of
`baselines/` like any other change, with the numbers each changed case kept before and after.

The stock universe: NSE's **Nifty 500** constituents, as `nifty500.csv`
(columns `Company Name`, `Industry`, `Symbol`, `Series`, `ISIN Code`).

`asr ingest instruments` downloads this file from NSE automatically when it is absent;
`--refresh` re-downloads it (NSE reshuffles the index periodically). The committed copy
is a snapshot, so a fresh clone can run without hitting NSE.

If NSE ever blocks the download, save the CSV here by hand — the code uses whatever
file is present.

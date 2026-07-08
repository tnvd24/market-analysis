Drop the NSE **Nifty 500** constituents CSV here as `nifty500.csv`.

Get it from NSE's index constituents download (the CSV with `Symbol` and
`ISIN Code` columns). `asr.ingest.instruments.load_universe()` reads this file.

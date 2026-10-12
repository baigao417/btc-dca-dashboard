# Third-Party Notices

No vendored third-party source code, fonts, screenshots, market datasets,
downloaded installers, or media are distributed in this repository.

The application uses Python's standard library from the user's Python runtime;
the runtime is not bundled. Optional `tzdata` is declared in `requirements.txt`
and is not vendored. Its installed package supplies its own license notices.

Optional live mode requests public market endpoints operated by Binance and
CoinGecko. No provider responses or historical datasets are distributed.
Users must independently review provider terms, availability and data rights
before enabling these endpoints. This project is not affiliated with them.

Generic webhooks require a user-supplied HTTPS receiver. Platform-specific
bot formats and credentials are not included.

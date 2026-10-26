# Real Exchange Rate Explorer

A Streamlit dashboard for an International Finance assignment at Northwestern University's Kellogg School of Management. Select any two countries (or the euro area) and an inclusive monthly date range, inspect real and nominal bilateral exchange rates, and export observations/calculations to CSV. Default: United States → Euro area, entire overlapping history from January 1976 onward where available.

## Methodology

- IMF monthly CPI index levels: `IMF.STA/CPI`, key `{CODE}.CPI._T.IX.M`.
- IMF period-average exchange rates (local currency units per USD): `IMF.STA/ER`, key `{CODE}.XDC_USD.PA_RT.M`. US dollar per US dollar is exactly 1.
- Let `E1`, `E2` be currency units per USD and `P1`, `P2` be the two CPI levels.
- Nominal rate `S = E2/E1`: **Country 2 currency per one Country 1 currency**.
- Real rate `RER = S × P1/P2`.
- `RER*` = arithmetic average of RER over valid selected months.
- `Real Index = 100 × RER/RER*`; its selected-period average is 100.
- Missing months are excluded; no interpolation, fallback providers, or synthetic data.

A real index above/below 100 is relative to the selected-period average, **not** evidence on its own that a currency is economically over/undervalued. Differences in CPI index bases and currency redenominations can cause structural breaks. The app warns about these limitations; it does not automatically correct them.

## Run locally (Python 3.12)

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

No credentials, local datasets, or databases are required. Internet access to `api.imf.org` is required to fetch live data.

## Deploy to Streamlit Community Cloud

1. Create a GitHub repository containing exactly `app.py`, `requirements.txt`, and `README.md` at its root.
2. Visit [share.streamlit.io](https://share.streamlit.io/) and choose **Create app** → **Deploy a public app from GitHub**.
3. Select the repository, branch, and `app.py` as the entrypoint.
4. Under **Advanced settings**, choose **Python 3.12**; deploy the app. (The location/name of settings can change.)
5. Streamlit installs dependencies from `requirements.txt` automatically. No secrets configuration is needed.

## Data availability and troubleshooting

IMF data are queried through SDMX 3.0 at `https://api.imf.org/external/sdmx/3.0` using its CSV response format and the latest dataflow versions. Data source: [IMF Data Portal](https://data.imf.org/) and [IMF API information](https://data.imf.org/en/Resource-Pages/IMF-API).

- **No data for a country**: a listed country/territory may not have either required IMF monthly series, especially for historical periods. Choose another pair; no alternate source is silently substituted.
- **No overlap / empty date window**: country-specific CPI and FX series may begin and end in different months; choose a range with valid common observations.
- **Timeout or HTTP error**: check connectivity, API maintenance, and IMF's published key/flow/dimension definitions; try again later. Cached data can remain available for up to 12 hours.
- **Unexpected response format**: the IMF may have changed SDMX response fields, format negotiation, or API syntax. Inspect the IMF API documentation and confirm the CPI/ER dataflows.
- **Unusual historical jump**: check for index rebasing, methodology changes, or currency denomination changes; such events are not silently adjusted.

**Verification note (2026-10-25):** The latest IMF dataflow names, series-key structures, and `text/csv` SDMX request convention were corroborated against published documentation and publicly indexed examples. Live API requests were *not* executable in the authoring environment because outbound DNS/network access was unavailable. The app has therefore **not** been end-to-end verified against actual IMF responses in that environment. Deploy and test USA/G163 plus at least one non-USD pair to confirm live API compatibility. Mathematical logic and parser behavior can be exercised locally with synthetic fixtures without representing those as live validation.

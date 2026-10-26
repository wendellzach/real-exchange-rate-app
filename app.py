"""Kellogg Real Exchange Rate Explorer — IMF SDMX 3.0, Python 3.12."""
from __future__ import annotations

import io
import re
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pycountry
import requests
import streamlit as st
from babel.numbers import get_territory_currencies

API_ROOT = "https://api.imf.org/external/sdmx/3.0"
PURPLE, DARK, PALE = "#4F2582", "#35165C", "#F3EFF8"
TEAL = "#0D9488"
FIRST_MONTH = pd.Period("1976-01", freq="M")
# Countries not necessarily IMF members/with coverage are selectable; the app reports unavailable series.
COUNTRIES = {c.alpha_3: c.name for c in pycountry.countries if hasattr(c, "alpha_3")}
COUNTRIES.update({"G163": "Euro area", "USA": "United States", "GBR": "United Kingdom", "KOR": "South Korea", "RUS": "Russia", "TUR": "Türkiye", "VNM": "Vietnam", "CZE": "Czechia", "TWN": "Taiwan"})
COUNTRY_CODES = sorted(COUNTRIES, key=lambda k: (k != "USA", k != "G163", COUNTRIES[k].lower()))


class IMFError(Exception):
    """An upstream data failure, distinct from a missing series."""


class NoSeriesError(Exception):
    """An IMF request succeeded but had no usable observations."""


def currency(code: str) -> str:
    if code == "G163":
        return "EUR"
    if code == "USA":
        return "USD"
    try:
        alpha2 = pycountry.countries.get(alpha_3=code).alpha_2
        values = get_territory_currencies(alpha2, tender=True)
        return values[0] if values else code
    except (AttributeError, LookupError, TypeError):
        return code


def to_month(series: pd.Series) -> pd.Series:
    s = series.astype("string").str.strip()
    # SDMX '2024-M01', conventional '2024-01', and '2024-01-01'.
    normalized = s.str.replace(r"^(\d{4})-M(\d{2})$", r"\1-\2", regex=True)
    parsed = pd.to_datetime(normalized, errors="coerce")
    return parsed.dt.to_period("M")


def parse_imf_csv(content: str, code: str, indicator: str, transformation: str) -> pd.Series:
    try:
        data = pd.read_csv(io.StringIO(content), dtype=str, low_memory=False)
    except (pd.errors.ParserError, ValueError, UnicodeError) as exc:
        raise IMFError("The IMF returned a CSV document that could not be read.") from exc
    data.columns = data.columns.astype(str).str.strip().str.upper()
    needed = {"TIME_PERIOD", "OBS_VALUE", "COUNTRY", "INDICATOR", "FREQUENCY", "TYPE_OF_TRANSFORMATION"}
    if not needed.issubset(data.columns):
        raise IMFError("The IMF response has unexpected columns. Check the dataflow/version or API format.")
    data = data.loc[
        (data["COUNTRY"] == code)
        & (data["INDICATOR"] == indicator)
        & (data["TYPE_OF_TRANSFORMATION"] == transformation)
        & (data["FREQUENCY"] == "M")
    ].copy()
    if data.empty:
        raise NoSeriesError(f"IMF has no monthly {indicator} observations for {COUNTRIES.get(code, code)}.")
    data["Month"] = to_month(data["TIME_PERIOD"])
    data["value"] = pd.to_numeric(data["OBS_VALUE"], errors="coerce")
    data = data.loc[data["Month"].notna() & np.isfinite(data["value"]) & (data["value"] > 0)]
    data = data.loc[data["Month"] >= FIRST_MONTH]
    if data.empty:
        raise NoSeriesError(f"No positive monthly {indicator} values from January 1976 for {COUNTRIES.get(code, code)}.")
    if data["Month"].duplicated().any():
        duplicates = data.loc[data["Month"].duplicated(keep=False), ["Month", "value"]]
        if duplicates.groupby("Month")["value"].nunique().max() > 1:
            raise IMFError(f"Conflicting IMF observations were returned for {code}, {indicator}.")
        data = data.drop_duplicates(subset="Month")
    result = data.set_index("Month")["value"].sort_index()
    result.index = pd.PeriodIndex(result.index, freq="M")
    return result.astype(float)


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def imf_series(code: str, flow: str, indicator: str, transform: str) -> pd.Series:
    """Fetch a single SDMX 3.0 monthly series as SDMX CSV (no substitutions)."""
    key = f"{code}.{indicator}.{transform}.M"
    # IMF SDMX 3.0: /data/dataflow/{agency}/{flow}/{latest}/{key}.
    url = f"{API_ROOT}/data/dataflow/IMF.STA/{flow}/~/{key}"
    try:
        response = requests.get(
            url,
            params={"c[TIME_PERIOD]": "ge:1976-M01"},
            headers={"Accept": "text/csv", "User-Agent": "Kellogg-RER-Explorer/1.0"},
            timeout=(10, 45),
        )
    except requests.exceptions.RequestException as exc:
        raise IMFError(f"Could not connect to the IMF for {code} ({flow}). Check internet access and try again.") from exc
    if response.status_code in (204, 404):
        raise NoSeriesError(f"IMF returned no {flow} series for {COUNTRIES.get(code, code)} ({key}).")
    if not response.ok:
        raise IMFError(f"IMF server returned HTTP {response.status_code} for {flow} ({code}).")
    if not response.content.strip():
        raise NoSeriesError(f"IMF returned an empty {flow} series for {COUNTRIES.get(code, code)}.")
    return parse_imf_csv(response.text, code, indicator, transform)


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def imf_cpi(code: str) -> pd.Series:
    key = f"{code}.CPI._T.IX.M"
    url = f"{API_ROOT}/data/dataflow/IMF.STA/CPI/~/{key}"
    try:
        response = requests.get(url, params={"c[TIME_PERIOD]": "ge:1976-M01"},
                                headers={"Accept": "text/csv", "User-Agent": "Kellogg-RER-Explorer/1.0"},
                                timeout=(10, 45))
    except requests.exceptions.RequestException as exc:
        raise IMFError(f"Could not connect to IMF CPI data for {COUNTRIES.get(code, code)}.") from exc
    if response.status_code in (204, 404):
        raise NoSeriesError(f"No CPI index series for {COUNTRIES.get(code, code)} ({key}).")
    if not response.ok:
        raise IMFError(f"IMF server returned HTTP {response.status_code} for CPI ({code}).")
    if not response.content.strip():
        raise NoSeriesError(f"IMF returned no CPI data for {COUNTRIES.get(code, code)}.")
    # CPI has an extra COICOP_1999 dimension; enforce exact key even in CSV parser.
    try:
        frame = pd.read_csv(io.StringIO(response.text), dtype=str, low_memory=False)
    except (pd.errors.ParserError, ValueError, UnicodeError) as exc:
        raise IMFError("IMF CPI response was not valid CSV.") from exc
    frame.columns = frame.columns.astype(str).str.strip().str.upper()
    required = {"COUNTRY", "INDEX_TYPE", "COICOP_1999", "TYPE_OF_TRANSFORMATION", "FREQUENCY", "TIME_PERIOD", "OBS_VALUE"}
    if not required.issubset(frame.columns):
        raise IMFError("IMF CPI format changed: expected SDMX dimension columns were not found.")
    frame = frame.loc[(frame["COUNTRY"] == code) & (frame["INDEX_TYPE"] == "CPI") &
                      (frame["COICOP_1999"] == "_T") & (frame["TYPE_OF_TRANSFORMATION"] == "IX") &
                      (frame["FREQUENCY"] == "M")]
    if frame.empty:
        raise NoSeriesError(f"No matching headline CPI index observations for {COUNTRIES.get(code, code)}.")
    frame = frame.copy()
    frame["Month"] = to_month(frame["TIME_PERIOD"])
    frame["value"] = pd.to_numeric(frame["OBS_VALUE"], errors="coerce")
    frame = frame.loc[frame["Month"].notna() & np.isfinite(frame["value"]) & (frame["value"] > 0) & (frame["Month"] >= FIRST_MONTH)]
    if frame.empty:
        raise NoSeriesError(f"No valid CPI observations since January 1976 for {COUNTRIES.get(code, code)}.")
    if frame["Month"].duplicated().any():
        if frame.groupby("Month")["value"].nunique().max() > 1:
            raise IMFError(f"Conflicting monthly CPI values for {COUNTRIES.get(code, code)}.")
        frame = frame.drop_duplicates("Month")
    result = frame.set_index("Month")["value"].sort_index().astype(float)
    result.index = pd.PeriodIndex(result.index, freq="M")
    return result


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def load_country(code: str) -> pd.DataFrame:
    cpi = imf_cpi(code)
    fx = pd.Series(1.0, index=cpi.index, dtype=float) if code == "USA" else imf_series(code, "ER", "XDC_USD", "PA_RT")
    return pd.concat([cpi.rename("CPI"), fx.rename("FX")], axis=1).sort_index()


def calculate_pair(first: pd.DataFrame, second: pd.DataFrame) -> pd.DataFrame:
    """Inner-join by exact month; no filling or interpolation."""
    data = first.rename(columns={"CPI": "P1", "FX": "E1"}).join(
        second.rename(columns={"CPI": "P2", "FX": "E2"}), how="inner"
    )
    data = data.replace([np.inf, -np.inf], np.nan).dropna(subset=["P1", "P2", "E1", "E2"])
    data = data.loc[(data[["P1", "P2", "E1", "E2"]] > 0).all(axis=1)].copy()
    if not data.empty:
        data["S"] = data["E2"] / data["E1"]
        data["RER"] = data["S"] * data["P1"] / data["P2"]
        data = data.loc[np.isfinite(data["S"]) & np.isfinite(data["RER"]) & (data["RER"] > 0)]
    return data.sort_index()


def calculate_sample(data: pd.DataFrame, start: pd.Period, end: pd.Period) -> tuple[pd.DataFrame, float]:
    if start > end:
        raise ValueError("Start month must be on or before end month.")
    sample = data.loc[(data.index >= start) & (data.index <= end)].copy()
    if sample.empty:
        return sample, float("nan")
    mean = float(sample["RER"].mean())
    sample["RER_avg"] = mean
    sample["Real_Index"] = 100 * sample["RER"] / mean
    return sample, mean


def chart(sample: pd.DataFrame, view: str, pair_label: str) -> go.Figure:
    # Full calendar grid prevents misleading straight lines over missing months.
    plot = sample.reindex(pd.period_range(sample.index.min(), sample.index.max(), freq="M"))
    dates = plot.index.to_timestamp()
    both = view == "Both"
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    if view in ("Real", "Both"):
        fig.add_trace(go.Scatter(x=dates, y=plot["Real_Index"], mode="lines", name="Real index",
                                 connectgaps=False, line=dict(color=PURPLE, width=2.8),
                                 hovertemplate="%{x|%b %Y}<br>Real index: %{y:.2f}<extra></extra>"), secondary_y=False)
        fig.add_hline(y=100, line_dash="dash", line_color="#A69BB5", opacity=0.9)
    if view in ("Nominal", "Both"):
        fig.add_trace(go.Scatter(x=dates, y=plot["S"], mode="lines", name="Nominal rate",
                                 connectgaps=False, line=dict(color=TEAL, width=2.4),
                                 hovertemplate=f"%{{x|%b %Y}}<br>Nominal: %{{y:.6g}} {pair_label}<extra></extra>"),
                      secondary_y=both)
    fig.update_layout(height=470, margin=dict(l=12, r=15, t=20, b=15),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="white",
                      font=dict(family="Arial", color="#20202A"), hovermode="x unified",
                      legend=dict(orientation="h", y=1.12, x=0),
                      xaxis=dict(title="Month", showgrid=True, gridcolor="#F0EDF4"))
    fig.update_yaxes(title_text="Real Exchange Rate Index" if view != "Nominal" else f"Nominal rate ({pair_label})",
                     showgrid=True, gridcolor="#F0EDF4", secondary_y=False)
    if both:
        fig.update_yaxes(title_text=f"Nominal rate ({pair_label})", showgrid=False, secondary_y=True)
    return fig


def csv_export(sample: pd.DataFrame) -> bytes:
    export = sample.reset_index().rename(columns={
        "index": "Month", "P1": "Country 1 CPI", "P2": "Country 2 CPI",
        "E1": "Country 1 local currency per USD", "E2": "Country 2 local currency per USD",
        "S": "Nominal exchange rate S(t)", "RER": "Real exchange rate RER(t)",
        "RER_avg": "Sample-average RER*", "Real_Index": "Normalized Real Index(t)",
    })
    export["Month"] = export["Month"].astype(str)
    return export.to_csv(index=False, float_format="%.16g").encode("utf-8-sig")


def main() -> None:
    st.set_page_config(page_title="Real Exchange Rate Explorer | Kellogg", page_icon="📈", layout="wide")
    st.markdown(f"""<style>
        .stApp {{background: #FAFAFC; color: #20202A;}}
        [data-testid="stSidebar"] {{background: {PALE};}}
        h1, h2, h3 {{color: {DARK};}}
        div[data-testid="stMetric"] {{background: white; border: 1px solid #EAE6F0; border-radius: 13px;
          padding: 14px 18px; box-shadow: 0 3px 14px rgba(53,22,92,0.04);}}
        div[data-testid="stMetricLabel"] {{color: #6B7280;}}
        div[data-testid="stMetricValue"] {{color: {DARK};}}
        .stButton button[kind="primary"], .stDownloadButton button {{background: {PURPLE}; color: white; border-radius: 9px; border: 0;}}
        .stButton button[kind="primary"]:hover, .stDownloadButton button:hover {{background: {DARK}; color: white;}}
        .hero {{border-left: 5px solid {PURPLE}; padding: 2px 0 2px 18px; margin: 8px 0 22px;}}
        .hero h1 {{margin: 0; font-size: 2.25rem;}}
        .hero p {{margin: 7px 0 0; color: #6B7280;}}
    </style>""", unsafe_allow_html=True)
    st.markdown('<div class="hero"><h1>Real Exchange Rate Explorer</h1>'
                '<p>Kellogg School of Management | International Finance</p></div>', unsafe_allow_html=True)
    st.sidebar.header("Explore currencies")
    a = st.sidebar.selectbox("Country 1", COUNTRY_CODES, index=COUNTRY_CODES.index("USA"),
                             format_func=lambda c: COUNTRIES[c], key="country1")
    b = st.sidebar.selectbox("Country 2", COUNTRY_CODES, index=COUNTRY_CODES.index("G163"),
                             format_func=lambda c: COUNTRIES[c], key="country2")
    if a == b:
        st.info("Select two different countries to explore their exchange rate.")
        return
    curr1, curr2 = currency(a), currency(b)
    st.sidebar.caption(f"Direction: 1 {curr1} → {curr2} ({curr2} per {curr1})")
    with st.spinner("Retrieving IMF monthly CPI and exchange rates…"):
        try:
            first = load_country(a)
            second = load_country(b)
            all_data = calculate_pair(first, second)
        except (IMFError, NoSeriesError) as exc:
            st.error(str(exc))
            st.info("The app uses IMF observations only. Try a different country or retry after checking IMF availability.")
            return
    if all_data.empty:
        st.warning("No overlapping valid monthly CPI and exchange-rate observations for this pair.")
        return
    months = pd.period_range(all_data.index.min(), all_data.index.max(), freq="M")
    if st.session_state.get("pair") != (a, b):
        old_start = st.session_state.get("start")
        old_end = st.session_state.get("end")
        st.session_state["start"] = max(months.min(), min(old_start, months.max())) if isinstance(old_start, pd.Period) else months.min()
        st.session_state["end"] = max(months.min(), min(old_end, months.max())) if isinstance(old_end, pd.Period) else months.max()
        if st.session_state["start"] > st.session_state["end"]:
            st.session_state["start"], st.session_state["end"] = months.min(), months.max()
        st.session_state["pair"] = (a, b)
    st.sidebar.selectbox("Start month (inclusive)", list(months), format_func=lambda p: p.strftime("%b %Y"), key="start")
    st.sidebar.selectbox("End month (inclusive)", list(months), format_func=lambda p: p.strftime("%b %Y"), key="end")
    if st.session_state.start > st.session_state.end:
        st.warning("The start month must be earlier than or equal to the end month.")
        return
    sample, mean = calculate_sample(all_data, st.session_state.start, st.session_state.end)
    if sample.empty:
        st.warning("There are no valid common observations in the selected range. Adjust the dates.")
        return
    latest = sample.iloc[-1]
    last_month = str(sample.index.max())
    st.caption(f"**{COUNTRIES[a]} / {COUNTRIES[b]}** · {sample.index.min()} to {last_month} · "
               f"1 {curr1} = {latest['S']:.6g} {curr2} as of {last_month}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Latest Real Index", f"{latest['Real_Index']:,.2f}")
    c2.metric(f"Latest Nominal ({curr2}/{curr1})", f"{latest['S']:,.5g}")
    c3.metric("Selected-period average RER*", f"{mean:,.6g}")
    c4.metric("Valid monthly observations", f"{len(sample):,}")
    st.markdown("### Historical exchange rates")
    view = st.segmented_control("Series", ["Real", "Nominal", "Both"], default="Real", key="chart_view")
    st.plotly_chart(chart(sample, view or "Real", f"{curr2}/{curr1}"), use_container_width=True,
                    config={"displaylogo": False, "toImageButtonOptions": {"format": "png", "filename": "RER_chart"}})
    st.download_button("Download CSV", csv_export(sample),
                       file_name=f"RER_{a}_{b}_{sample.index.min()}_{sample.index.max()}.csv",
                       mime="text/csv", type="primary")
    with st.expander("Methodology & Data Sources"):
        st.markdown(f"""**Nominal exchange rate:** IMF monthly average local-currency per USD series, with
        USA represented by 1 USD/USD. Define E₁ and E₂ for countries 1 and 2, respectively.
        **S = E₂ / E₁** is {curr2} per {curr1} (not the reverse). 

        **Real exchange rate:** **RER = S × CPI₁ / CPI₂**, using positive CPI index levels from the
        same month. **RER*** is the arithmetic mean of valid RER observations in your selected
        months. **Real Index = 100 × RER / RER***, so its sample mean is 100.

        Values over or under 100 describe a deviation from *this selected sample's average*;
        they do not prove fundamental overvaluation or undervaluation. Missing months are
        excluded rather than filled. Country coverage varies. Historical CPI rebasing and
        currency redenominations may affect comparisons; they are not automatically corrected.

        Data: [IMF Data Portal](https://data.imf.org/) · [IMF API documentation](https://data.imf.org/en/Resource-Pages/IMF-API).
        CPI flow: `IMF.STA/CPI` (`COUNTRY.CPI._T.IX.M`); FX flow: `IMF.STA/ER`
        (`COUNTRY.XDC_USD.PA_RT.M`). Cached for up to 12 hours.""")


if __name__ == "__main__":
    main()

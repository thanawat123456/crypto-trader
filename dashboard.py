"""Dashboard แสดงผลบอทเทรด — รันบน Mac, ดึงข้อมูลจาก GCP VM มาแสดง

เปิดด้วย:  ./run-dashboard.sh   (หรือ  .venv/bin/streamlit run dashboard.py)
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import timezone

import pandas as pd
import streamlit as st

from crypto_trader.config import load_config

DATA_DIR = "dashboard_data"
STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")
JOURNAL_FILE = os.path.join(DATA_DIR, "trade_journal.csv")
APP_DIR = "/home/ubuntu/crypto-trader"

cfg = load_config("ไม่มีไฟล์.yaml")
INITIAL_CASH = float(cfg.get("paper", {}).get("initial_cash", 300))
WATCHLIST = list(cfg.get("bot", {}).get("symbols") or [cfg["defaults"]["symbol"]])

st.set_page_config(page_title="Crypto Bot Dashboard", page_icon="🤖", layout="wide")


def sync_from_vm(ip: str, key: str) -> list[tuple[str, bool, str]]:
    """ดึง state/journal จาก GCP VM มาไว้ใน dashboard_data"""
    os.makedirs(DATA_DIR, exist_ok=True)
    base = f"ubuntu@{ip}:{APP_DIR}"
    results = []
    for fn in ("bot_state.json", "trade_journal.csv", "validation_log.csv"):
        proc = subprocess.run(
            [
                "scp", "-i", key, "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10", f"{base}/{fn}", DATA_DIR + "/",
            ],
            capture_output=True, text=True,
            check=False,
        )
        missing_optional = proc.returncode != 0 and "No such file" in proc.stderr
        if missing_optional and fn in {"trade_journal.csv", "validation_log.csv"}:
            results.append((fn, True, "ยังไม่มีไฟล์บน VM"))
        else:
            results.append((fn, proc.returncode == 0, proc.stderr.strip()))
    return results


def load_state_file() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_trades() -> pd.DataFrame:
    if not os.path.exists(JOURNAL_FILE):
        return pd.DataFrame(columns=["timestamp", "symbol", "timeframe", "side", "price", "amount", "reason", "pnl"])
    try:
        df = pd.read_csv(JOURNAL_FILE)
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["timestamp", "symbol", "timeframe", "side", "price", "amount", "reason", "pnl"])
    if "timestamp" in df:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    for col in ("price", "amount", "pnl"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_validation_log() -> pd.DataFrame:
    path = os.path.join(DATA_DIR, "validation_log.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    if "timestamp" in df:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    return df


def _state_key(symbol: str, timeframe: str = "1h") -> str:
    return f"{symbol}|{timeframe}"


def _legacy_symbols(symbol: str) -> list[str]:
    """รองรับ state/journal เก่าที่เคยใช้ /USDT ก่อนย้ายมา Kraken /USD"""
    out = [symbol]
    if symbol.endswith("/USD"):
        out.append(symbol + "T")
    elif symbol.endswith("/USDT"):
        out.append(symbol.replace("/USDT", "/USD"))
    return out


@st.cache_data(ttl=180, show_spinner=False)
def get_market_snapshot(symbol: str) -> dict:
    """ดึงราคาล่าสุดและ momentum เบื้องต้นจาก Kraken"""
    from crypto_trader.data import fetch_ohlcv, latest_price, make_exchange

    local_cfg = load_config("ไม่มีไฟล์.yaml")
    local_cfg["exchange"]["name"] = "kraken"
    ex = make_exchange(local_cfg)
    price = latest_price(ex, symbol)
    df = fetch_ohlcv(ex, symbol, "1h", 80)
    close = df["close"]
    chg_1h = float(close.iloc[-1] / close.iloc[-2] - 1) if len(close) > 2 else 0.0
    chg_24h = float(close.iloc[-1] / close.iloc[-25] - 1) if len(close) > 25 else 0.0
    chg_7d = float(close.iloc[-1] / close.iloc[-24 * 7 - 1] - 1) if len(close) > 24 * 7 else None
    return {"price": float(price), "chg_1h": chg_1h, "chg_24h": chg_24h, "chg_7d": chg_7d}


@st.cache_data(ttl=300, show_spinner=False)
def get_ohlcv(symbol: str) -> pd.DataFrame:
    from crypto_trader.data import fetch_ohlcv, make_exchange

    local_cfg = load_config("ไม่มีไฟล์.yaml")
    local_cfg["exchange"]["name"] = "kraken"
    ex = make_exchange(local_cfg)
    return fetch_ohlcv(ex, symbol, "1h", 300)


@st.cache_data(ttl=300, show_spinner=False)
def get_close_matrix(symbols: tuple[str, ...]) -> pd.DataFrame:
    data = {}
    for sym in symbols:
        try:
            df = get_ohlcv(sym)
            data[sym] = df["close"]
        except Exception:  # noqa: BLE001
            continue
    if not data:
        return pd.DataFrame()
    closes = pd.DataFrame(data).dropna(how="all")
    return closes.ffill()


def _fmt_pct(x: float | None) -> str:
    if x is None or pd.isna(x):
        return "n/a"
    return f"{x:+.2%}"


def _symbol_trade_stats(trades: pd.DataFrame, symbol: str) -> dict:
    symbols = _legacy_symbols(symbol)
    if trades.empty or "symbol" not in trades:
        return {"buy": 0, "sell": 0, "pnl": 0.0, "wr": 0.0, "last": "-"}
    t = trades[trades["symbol"].isin(symbols)].copy()
    sells = t[t["side"].astype(str).str.upper() == "SELL"] if not t.empty else pd.DataFrame()
    pnl = float(sells["pnl"].fillna(0).sum()) if not sells.empty and "pnl" in sells else 0.0
    wins = int((sells["pnl"].fillna(0) > 0).sum()) if not sells.empty and "pnl" in sells else 0
    wr = wins / len(sells) * 100 if len(sells) else 0.0
    buys = int((t["side"].astype(str).str.upper() == "BUY").sum()) if not t.empty else 0
    last = "-"
    if not t.empty:
        row = t.sort_values("timestamp").iloc[-1]
        last = f"{row.get('side')} {float(row.get('price') or 0):,.4g} ({row.get('reason') or '-'})"
    return {"buy": buys, "sell": len(sells), "pnl": pnl, "wr": wr, "last": last}


def build_symbol_rows(state_data: dict, trades: pd.DataFrame, include_market: bool) -> pd.DataFrame:
    rows = []
    for sym in WATCHLIST:
        st_record = {}
        for alias in _legacy_symbols(sym):
            st_record = state_data.get(_state_key(alias), {})
            if st_record:
                break
        in_pos = bool(st_record.get("in_position", False))
        entry = st_record.get("entry_price")
        amount = st_record.get("amount")
        peak = st_record.get("peak_price")
        updated_at = st_record.get("updated_at")

        price = None
        chg_1h = chg_24h = chg_7d = None
        unreal = None
        if include_market:
            try:
                snap = get_market_snapshot(sym)
                price = snap["price"]
                chg_1h, chg_24h, chg_7d = snap["chg_1h"], snap["chg_24h"], snap["chg_7d"]
                if in_pos and entry and amount:
                    unreal = (price - float(entry)) * float(amount)
            except Exception:  # noqa: BLE001
                pass

        stats = _symbol_trade_stats(trades, sym)
        rows.append({
            "symbol": sym,
            "status": "ถืออยู่" if in_pos else "ถือเงินสด",
            "price": price,
            "1h": _fmt_pct(chg_1h),
            "24h": _fmt_pct(chg_24h),
            "7d": _fmt_pct(chg_7d),
            "entry": entry,
            "amount": amount,
            "peak": peak,
            "unrealized": unreal,
            "realized": stats["pnl"],
            "BUY": stats["buy"],
            "SELL": stats["sell"],
            "WR%": stats["wr"],
            "last_trade": stats["last"],
            "updated_at": updated_at,
        })
    return pd.DataFrame(rows)


def style_symbol_table(df: pd.DataFrame):
    df = df.copy()
    numeric_cols = ["price", "entry", "amount", "peak", "unrealized", "realized", "WR%"]
    for col in numeric_cols:
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.style.format({
        "price": "{:,.4f}",
        "entry": "{:,.4f}",
        "amount": "{:,.8f}",
        "peak": "{:,.4f}",
        "unrealized": "{:+,.2f}",
        "realized": "{:+,.2f}",
        "WR%": "{:.0f}%",
    }, na_rep="-").map(
        lambda v: "color: #16a34a" if isinstance(v, (int, float)) and v > 0 else (
            "color: #dc2626" if isinstance(v, (int, float)) and v < 0 else ""
        ),
        subset=["unrealized", "realized"],
    )


# ----------------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------------
st.sidebar.title("⚙️ ตั้งค่า")
vm_ip = st.sidebar.text_input("VM IP", value=os.environ.get("VM_IP", "34.42.212.100"))
ssh_key = st.sidebar.text_input("SSH key", value=os.path.expanduser("~/.ssh/id_rsa"))
include_market = st.sidebar.checkbox("ดึงราคาตลาดสด", value=True)

if st.sidebar.button("🔄 ดึงข้อมูลล่าสุดจาก VM", use_container_width=True):
    with st.spinner("กำลังดึงข้อมูล..."):
        for fn, ok, err in sync_from_vm(vm_ip, ssh_key):
            if ok:
                if err:
                    st.sidebar.info(f"{fn} — {err}")
                else:
                    st.sidebar.success(fn)
            else:
                st.sidebar.warning(f"{fn} — {err or 'ดึงไม่ได้ (อาจยังไม่มีไฟล์)'}")
        st.cache_data.clear()

if os.path.exists(STATE_FILE):
    mtime = pd.Timestamp(os.path.getmtime(STATE_FILE), unit="s", tz="UTC").tz_convert("Asia/Bangkok")
    st.sidebar.caption(f"ข้อมูล state ล่าสุด: {mtime:%Y-%m-%d %H:%M} เวลาไทย")
else:
    st.sidebar.info("ยังไม่มีข้อมูล — กดปุ่มดึงจาก VM ก่อน")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
st.title("🤖 Crypto Bot Dashboard")
st.caption("ภาพรวม paper portfolio, สถานะ 11 เหรียญ, PnL, ความเคลื่อนไหว และประวัติเทรด")

state_data = load_state_file()
portfolio = state_data.get("__portfolio__", {"cash": INITIAL_CASH, "realized_pnl": 0.0})
trades = load_trades()
validation = load_validation_log()
symbol_df = build_symbol_rows(state_data, trades, include_market)

cash = float(portfolio.get("cash", INITIAL_CASH))
realized = float(portfolio.get("realized_pnl", 0.0))
open_count = int((symbol_df["status"] == "ถืออยู่").sum()) if not symbol_df.empty else 0
sells_total = int(symbol_df["SELL"].sum()) if not symbol_df.empty else 0
wins_total = 0
if not trades.empty and "side" in trades:
    sells = trades[trades["side"].astype(str).str.upper() == "SELL"].copy()
    if not sells.empty:
        wins_total = int((pd.to_numeric(sells["pnl"], errors="coerce").fillna(0) > 0).sum())
win_rate = wins_total / sells_total * 100 if sells_total else 0.0
unreal_total = float(symbol_df["unrealized"].fillna(0).sum()) if not symbol_df.empty else 0.0
equity_now = cash + sum(
    float(r["amount"] or 0) * float(r["price"] or 0)
    for _, r in symbol_df.iterrows() if r["status"] == "ถืออยู่" and pd.notna(r["price"])
)

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Paper cash", f"{cash:,.2f}")
c2.metric("Equity ประมาณ", f"{equity_now:,.2f}", f"{(equity_now / INITIAL_CASH - 1) * 100:+.2f}%")
c3.metric("Realized PnL", f"{realized:+,.2f}", f"{realized / INITIAL_CASH * 100:+.2f}%")
c4.metric("Unrealized", f"{unreal_total:+,.2f}")
c5.metric("ถืออยู่", f"{open_count}/{len(WATCHLIST)}")
c6.metric("Win rate", f"{win_rate:.0f}%", f"SELL {sells_total}")

st.divider()

st.subheader("📊 Market movement แยกรายเหรียญ")
if include_market:
    closes = get_close_matrix(tuple(WATCHLIST))
    if closes.empty:
        st.warning("โหลดราคาตลาดไม่ได้ ลองรีเฟรชหรือปิด/เปิดตัวเลือกดึงราคาตลาดสด")
    else:
        for row_symbols in [WATCHLIST[i:i + 2] for i in range(0, len(WATCHLIST), 2)]:
            cols = st.columns(len(row_symbols))
            for col, sym in zip(cols, row_symbols):
                with col:
                    st.caption(sym)
                    if sym in closes:
                        st.line_chart(closes[sym].tail(120), height=220)
                    else:
                        st.info("ไม่มีข้อมูลราคา")
else:
    st.info("เปิดตัวเลือก 'ดึงราคาตลาดสด' ใน sidebar เพื่อดูกราฟ market movement รายเหรียญ")

st.divider()

st.subheader("🌐 ภาพรวมทุกเหรียญ")
if symbol_df.empty:
    st.info("ยังไม่มีข้อมูล")
else:
    view_cols = [
        "symbol", "status", "price", "1h", "24h", "7d", "entry", "amount",
        "unrealized", "realized", "BUY", "SELL", "WR%", "last_trade",
    ]
    st.dataframe(style_symbol_table(symbol_df[view_cols]), use_container_width=True, hide_index=True)

left, right = st.columns([1.1, 1])
with left:
    st.subheader("📈 Equity curve")
    if not trades.empty and "side" in trades:
        sells = trades[trades["side"].astype(str).str.upper() == "SELL"].copy()
        if not sells.empty:
            sells = sells.sort_values("timestamp")
            sells["equity"] = INITIAL_CASH + sells["pnl"].fillna(0).cumsum()
            st.line_chart(sells.set_index("timestamp")["equity"], height=280)
        else:
            st.info("ยังไม่มีไม้ SELL ที่ปิดแล้ว")
    else:
        st.info("ยังไม่มี trade journal — ปกติถ้าบอทยังไม่เคย BUY/SELL หลังเริ่มรอบใหม่")

with right:
    st.subheader("🏆 สรุปรายเหรียญ")
    if not symbol_df.empty:
        perf = symbol_df[["symbol", "realized", "unrealized", "SELL", "WR%"]].copy()
        for col in ("realized", "unrealized", "SELL", "WR%"):
            perf[col] = pd.to_numeric(perf[col], errors="coerce")
        perf["total_pnl"] = perf["realized"].fillna(0) + perf["unrealized"].fillna(0)
        perf = perf.sort_values("total_pnl", ascending=False)
        st.dataframe(
            perf[["symbol", "total_pnl", "realized", "unrealized", "SELL", "WR%"]].style.format({
                "total_pnl": "{:+,.2f}", "realized": "{:+,.2f}",
                "unrealized": "{:+,.2f}", "WR%": "{:.0f}%",
            }, na_rep="-"),
            use_container_width=True, hide_index=True,
        )

st.divider()

st.subheader("🕯️ กราฟราคา + จุดเทรด")
sym = st.selectbox("เลือกเหรียญ", WATCHLIST)
try:
    import plotly.graph_objects as go

    df_price = get_ohlcv(sym)
    fig = go.Figure(data=[go.Candlestick(
        x=df_price.index, open=df_price["open"], high=df_price["high"],
        low=df_price["low"], close=df_price["close"], name=sym,
    )])
    if not trades.empty and "symbol" in trades:
        tr = trades[trades["symbol"].isin(_legacy_symbols(sym))]
        for side, color, marker in [("BUY", "lime", "triangle-up"), ("SELL", "red", "triangle-down")]:
            pts = tr[tr["side"].astype(str).str.upper() == side]
            if not pts.empty:
                fig.add_trace(go.Scatter(
                    x=pts["timestamp"], y=pts["price"], mode="markers", name=side,
                    marker=dict(color=color, size=12, symbol=marker),
                ))
    fig.update_layout(height=460, xaxis_rangeslider_visible=False, margin=dict(t=10))
    st.plotly_chart(fig, use_container_width=True)
except Exception as e:  # noqa: BLE001
    st.warning(f"โหลด candlestick ไม่ได้: {e}")
    try:
        df_price = get_ohlcv(sym)
        st.line_chart(df_price["close"], height=360)
        st.caption("fallback: แสดงเส้นราคาปิดแทน candlestick")
    except Exception as e2:  # noqa: BLE001
        st.error(f"โหลดกราฟราคาไม่ได้: {e2}")

st.divider()

st.subheader("📋 ประวัติเทรดล่าสุด")
if not trades.empty:
    show = trades.sort_values("timestamp", ascending=False).head(50).copy()
    if "timestamp" in show:
        show["timestamp"] = show["timestamp"].dt.tz_convert(timezone.utc).dt.strftime("%Y-%m-%d %H:%M UTC")
    st.dataframe(show, use_container_width=True, hide_index=True)
else:
    st.info("ยังไม่มีประวัติเทรด")

st.divider()

st.subheader("🔬 Validation ล่าสุด")
if validation.empty:
    st.info("ยังไม่มี validation_log.csv — จะมีหลัง routine validate ทำงานบน VM")
else:
    latest = validation.sort_values("timestamp", ascending=False).head(20).copy()
    if "timestamp" in latest:
        latest["timestamp"] = latest["timestamp"].dt.strftime("%Y-%m-%d %H:%M UTC")
    st.dataframe(latest, use_container_width=True, hide_index=True)

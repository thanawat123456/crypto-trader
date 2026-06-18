"""Dashboard แสดงผลบอทเทรด — รันบน Mac, ดึงข้อมูลจาก GCP VM มาแสดง

เปิดด้วย:  ./run-dashboard.sh   (หรือ  .venv/bin/streamlit run dashboard.py)
"""
from __future__ import annotations

import json
import os
import subprocess

import pandas as pd
import streamlit as st

DATA_DIR = "dashboard_data"
STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")
JOURNAL_FILE = os.path.join(DATA_DIR, "trade_journal.csv")
INITIAL_CASH = 300.0
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT"]
APP_DIR = "/home/ubuntu/crypto-trader"

st.set_page_config(page_title="Crypto Bot Dashboard", page_icon="🤖", layout="wide")


# ----------------------------------------------------------------------------
# ดึงข้อมูลจาก VM (scp) + โหลดไฟล์
# ----------------------------------------------------------------------------
def sync_from_vm(ip: str, key: str) -> list[tuple[str, bool, str]]:
    os.makedirs(DATA_DIR, exist_ok=True)
    base = f"ubuntu@{ip}:{APP_DIR}"
    results = []
    for fn in ("bot_state.json", "trade_journal.csv"):
        proc = subprocess.run(
            ["scp", "-i", key, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
             f"{base}/{fn}", DATA_DIR + "/"],
            capture_output=True, text=True,
        )
        results.append((fn, proc.returncode == 0, proc.stderr.strip()))
    return results


def load_portfolio() -> tuple[dict, list[tuple[str, dict]]]:
    if not os.path.exists(STATE_FILE):
        return {"cash": INITIAL_CASH, "realized_pnl": 0.0}, []
    try:
        data = json.load(open(STATE_FILE, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"cash": INITIAL_CASH, "realized_pnl": 0.0}, []
    pf = data.get("__portfolio__", {"cash": INITIAL_CASH, "realized_pnl": 0.0})
    positions = [
        (k, v) for k, v in data.items()
        if k != "__portfolio__" and isinstance(v, dict) and v.get("in_position")
    ]
    return pf, positions


def load_trades() -> pd.DataFrame:
    if not os.path.exists(JOURNAL_FILE):
        return pd.DataFrame()
    try:
        df = pd.read_csv(JOURNAL_FILE)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    if not df.empty and "timestamp" in df:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df


@st.cache_data(ttl=300, show_spinner=False)
def get_ohlcv(symbol: str) -> pd.DataFrame:
    """ดึงราคาจาก kraken (cache 5 นาที) — ใช้แพ็กเกจเดิมของโปรเจกต์"""
    from crypto_trader.config import load_config
    from crypto_trader.data import fetch_ohlcv, make_exchange
    cfg = load_config("ไม่มีไฟล์.yaml")
    cfg["exchange"]["name"] = "kraken"
    ex = make_exchange(cfg)
    return fetch_ohlcv(ex, symbol, "1h", 400)


# ----------------------------------------------------------------------------
# Sidebar — ตั้งค่า + ปุ่ม sync
# ----------------------------------------------------------------------------
st.sidebar.title("⚙️ ตั้งค่า")
vm_ip = st.sidebar.text_input("VM IP", value=os.environ.get("VM_IP", "34.42.212.100"))
ssh_key = st.sidebar.text_input("SSH key", value=os.path.expanduser("~/.ssh/id_rsa"))

if st.sidebar.button("🔄 ดึงข้อมูลล่าสุดจาก VM", use_container_width=True):
    with st.spinner("กำลังดึงข้อมูล..."):
        for fn, ok, err in sync_from_vm(vm_ip, ssh_key):
            if ok:
                st.sidebar.success(f"{fn}")
            else:
                st.sidebar.warning(f"{fn} — {err or 'ดึงไม่ได้ (อาจยังไม่มีไฟล์)'}")

if os.path.exists(STATE_FILE):
    mtime = pd.Timestamp(os.path.getmtime(STATE_FILE), unit="s", tz="UTC").tz_convert("Asia/Bangkok")
    st.sidebar.caption(f"ข้อมูลล่าสุด: {mtime:%Y-%m-%d %H:%M} (เวลาไทย)")
else:
    st.sidebar.info("ยังไม่มีข้อมูล — กดปุ่มดึงจาก VM ก่อน")


# ----------------------------------------------------------------------------
# หน้าหลัก
# ----------------------------------------------------------------------------
st.title("🤖 Crypto Bot Dashboard")

pf, positions = load_portfolio()
trades = load_trades()
cash = float(pf.get("cash", INITIAL_CASH))
realized = float(pf.get("realized_pnl", 0.0))

sells = pd.DataFrame()
if not trades.empty and "side" in trades:
    sells = trades[trades["side"].astype(str).str.upper() == "SELL"].copy()
    sells["pnl"] = pd.to_numeric(sells.get("pnl"), errors="coerce").fillna(0.0)

n_sells = len(sells)
wins = int((sells["pnl"] > 0).sum()) if n_sells else 0
win_rate = (wins / n_sells * 100) if n_sells else 0.0
total_return = realized / INITIAL_CASH * 100

# --- แถวเมตริก ---
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("เงินสด (paper)", f"{cash:,.2f}")
c2.metric("กำไรสะสม", f"{realized:+,.2f}", f"{total_return:+.2f}%")
c3.metric("ไม้ที่ปิดแล้ว", f"{n_sells}")
c4.metric("Win rate", f"{win_rate:.0f}%")
c5.metric("ถืออยู่ตอนนี้", f"{len(positions)} เหรียญ")

st.divider()

# --- กราฟ equity (กำไรสะสมตามเวลา) ---
st.subheader("📈 Equity curve (กำไรสะสม)")
if n_sells:
    eq = sells.sort_values("timestamp")[["timestamp", "pnl"]].copy()
    eq["equity"] = INITIAL_CASH + eq["pnl"].cumsum()
    st.line_chart(eq.set_index("timestamp")["equity"], height=280)
else:
    st.info("ยังไม่มีไม้ที่ปิด — equity ยังเท่าทุนเริ่มต้น (300) รอบอทเทรดก่อน")

# --- ไม้ที่ถืออยู่ ---
if positions:
    st.subheader("💼 ไม้ที่ถืออยู่")
    rows = [
        {"เหรียญ": k.split("|")[0], "ราคาเข้า": v.get("entry_price"),
         "จำนวน": v.get("amount"), "peak": v.get("peak_price")}
        for k, v in positions
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.divider()

# --- กราฟราคา + จุด BUY/SELL ---
st.subheader("🕯️ กราฟราคา + จุดเทรด")
sym = st.selectbox("เลือกเหรียญ", SYMBOLS)
try:
    import plotly.graph_objects as go

    df = get_ohlcv(sym)
    fig = go.Figure(
        data=[go.Candlestick(
            x=df.index, open=df["open"], high=df["high"],
            low=df["low"], close=df["close"], name=sym,
        )]
    )
    if not trades.empty and "symbol" in trades:
        tr = trades[trades["symbol"] == sym]
        for side, color, symbol_marker in [("BUY", "lime", "triangle-up"),
                                           ("SELL", "red", "triangle-down")]:
            pts = tr[tr["side"].astype(str).str.upper() == side]
            if not pts.empty:
                fig.add_trace(go.Scatter(
                    x=pts["timestamp"], y=pd.to_numeric(pts["price"], errors="coerce"),
                    mode="markers", name=side,
                    marker=dict(color=color, size=12, symbol=symbol_marker),
                ))
    fig.update_layout(height=480, xaxis_rangeslider_visible=False, margin=dict(t=10))
    st.plotly_chart(fig, use_container_width=True)
except Exception as e:  # noqa: BLE001
    st.warning(f"โหลดกราฟราคาไม่ได้ (network?): {e}")

# --- ตารางประวัติเทรด ---
st.divider()
st.subheader("📋 ประวัติเทรดล่าสุด")
if not trades.empty:
    show = trades.sort_values("timestamp", ascending=False).head(30)
    st.dataframe(show, use_container_width=True, hide_index=True)
else:
    st.info("ยังไม่มีประวัติเทรด")

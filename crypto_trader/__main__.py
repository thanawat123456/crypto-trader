"""จุดเริ่มของโปรแกรม — รันด้วย:  python -m crypto_trader <command>

คำสั่ง:
  fetch     ดึงข้อมูลราคาและแสดงตาราง/บันทึก CSV
  chart     วาดกราฟราคา + EMA + RSI เป็นไฟล์ PNG
  signal    เช็คสัญญาณล่าสุด (BUY/SELL/HOLD)
  backtest  ทดสอบกลยุทธ์ย้อนหลัง + กราฟ equity
  bot       รันบอทเทรด (dry-run / paper / live)
"""
from __future__ import annotations

import argparse
import sys

from .config import load_config
from .data import fetch_ohlcv, make_exchange
from .strategy import latest_signal


def _build_parser(cfg: dict) -> argparse.ArgumentParser:
    d = cfg["defaults"]
    parser = argparse.ArgumentParser(
        prog="crypto_trader", description="ชุดเครื่องมือเทรดคริปโตด้วย Python"
    )
    parser.add_argument("-c", "--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("symbol", nargs="?", default=d["symbol"], help="เช่น BTC/USDT")
        sp.add_argument("-t", "--timeframe", default=d["timeframe"])
        sp.add_argument("-l", "--limit", type=int, default=d["limit"])
        sp.add_argument(
            "--strategy",
            choices=["ema_cross", "rsi", "macd", "bb_squeeze", "rsi2", "heikin_stoch"],
            help="เลือกกลยุทธ์ทับค่าใน config",
        )

    for name in ("fetch", "chart", "signal", "backtest", "optimize", "walkforward", "bot"):
        sp = sub.add_parser(name)
        common(sp)
        if name == "bot":
            sp.add_argument("--once", action="store_true", help="รันรอบเดียวแล้วหยุด")
        if name in ("optimize", "walkforward"):
            sp.add_argument("--metric", choices=["total_return", "sharpe"],
                            default="total_return", help="จัดอันดับตามอะไร")
        if name == "optimize":
            sp.add_argument("--top", type=int, default=10, help="แสดงกี่อันดับ")
        if name == "walkforward":
            sp.add_argument("--train", type=int, default=300, help="จำนวนแท่งช่วง train")
            sp.add_argument("--test", type=int, default=100, help="จำนวนแท่งช่วง test")
            sp.add_argument("--step", type=int, default=100, help="เลื่อนหน้าต่างทีละกี่แท่ง")
    return parser


def main(argv=None) -> int:
    # อ่าน config ก่อน เพื่อใช้ค่า default ตอนสร้าง parser
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("-c", "--config", default="config.yaml")
    known, _ = pre.parse_known_args(argv)
    cfg = load_config(known.config)

    args = _build_parser(cfg).parse_args(argv)
    if getattr(args, "strategy", None):
        cfg["strategy"]["name"] = args.strategy

    safe = args.symbol.replace("/", "_")

    if args.command == "fetch":
        ex = make_exchange(cfg)
        df = fetch_ohlcv(ex, args.symbol, args.timeframe, args.limit)
        print(df.tail(20).to_string())
        out = f"{safe}_{args.timeframe}.csv"
        df.to_csv(out)
        print(f"\n💾 บันทึก {len(df)} แท่งลง {out}")

    elif args.command == "chart":
        from .plotting import plot_chart
        ex = make_exchange(cfg)
        df = fetch_ohlcv(ex, args.symbol, args.timeframe, args.limit)
        out = plot_chart(df, cfg, f"{safe}_{args.timeframe}.png")
        print(f"📈 บันทึกกราฟลง {out}")

    elif args.command == "signal":
        from . import alerts
        ex = make_exchange(cfg)
        df = fetch_ohlcv(ex, args.symbol, args.timeframe, args.limit)
        sig = latest_signal(df.iloc[:-1], cfg)
        price = float(df["close"].iloc[-1])
        alerts.notify(cfg, alerts.signal_message(args.symbol, args.timeframe, sig, price))

    elif args.command == "backtest":
        from .backtest import run_backtest
        from .plotting import plot_equity
        ex = make_exchange(cfg)
        df = fetch_ohlcv(ex, args.symbol, args.timeframe, args.limit)
        result = run_backtest(df, cfg, args.timeframe)
        print(result.summary())
        out = plot_equity(result.equity, f"equity_{safe}.png")
        print(f"📈 บันทึกกราฟ equity ลง {out}")

    elif args.command == "optimize":
        from .optimize import optimize_ema
        ex = make_exchange(cfg)
        df = fetch_ohlcv(ex, args.symbol, args.timeframe, args.limit)
        table = optimize_ema(df, cfg, args.timeframe, metric=args.metric)
        print(f"\n🔎 Optimize ema_cross | {args.symbol} {args.timeframe} "
              f"| จัดอันดับตาม {args.metric}\n")
        print(table.head(args.top).to_string(index=False))
        best = table.iloc[0]
        print(f"\n⭐ ดีที่สุด: fast={int(best['fast'])} slow={int(best['slow'])} "
              f"→ return={best['return_%']}% sharpe={best['sharpe']}")
        print("⚠️  ระวัง overfitting — ลองทดสอบกับช่วงเวลา/เหรียญอื่นก่อนใช้จริง")

    elif args.command == "walkforward":
        from .walkforward import summarize, walk_forward
        ex = make_exchange(cfg)
        df = fetch_ohlcv(ex, args.symbol, args.timeframe, args.limit)
        print(f"\n🔬 Walk-forward | {args.symbol} {args.timeframe} "
              f"| train={args.train} test={args.test} step={args.step}\n")
        table = walk_forward(df, cfg, args.timeframe, train=args.train,
                             test=args.test, step=args.step, metric=args.metric)
        if not table.empty:
            print(table.to_string(index=False))
        print("\n" + summarize(table))

    elif args.command == "bot":
        from .bot import run_bot
        if not cfg["exchange"].get("sandbox", True) and cfg["exchange"].get("api_key"):
            ans = input("⚠️  โหมด LIVE เงินจริง! พิมพ์ 'yes' เพื่อยืนยัน: ")
            if ans.strip().lower() != "yes":
                print("ยกเลิก")
                return 1
        ex = make_exchange(cfg)
        run_bot(ex, cfg, args.symbol, args.timeframe, once=args.once)

    return 0


if __name__ == "__main__":
    sys.exit(main())

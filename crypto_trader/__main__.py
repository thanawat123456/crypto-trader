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
            choices=["ema_cross", "rsi", "macd", "bb_squeeze", "rsi2", "heikin_stoch", "tsmom"],
            help="เลือกกลยุทธ์ทับค่าใน config",
        )

    for name in ("fetch", "chart", "signal", "backtest", "optimize", "walkforward",
                 "montecarlo", "pbo", "report", "validate", "scan", "status", "bot"):
        sp = sub.add_parser(name)
        common(sp)
        if name == "pbo":
            sp.add_argument("--mode", choices=["strategies", "ema"], default="strategies",
                            help="เทียบทุกกลยุทธ์ (strategies) หรือ grid ของ ema_cross (ema)")
            sp.add_argument("--blocks", type=int, default=12, help="จำนวนบล็อก S (คู่)")
        if name == "status":
            sp.add_argument("--notify", action="store_true", help="ส่งผลเข้า Discord ด้วย")
            sp.add_argument("--every-hours", type=float, default=0,
                            help="ทำเฉพาะถ้าผ่านมาแล้ว N ชม.ตั้งแต่ครั้งก่อน (0 = ทำทันที)")
        if name == "scan":
            sp.add_argument("--quote", default="USD", help="เหรียญฝั่ง quote (Kraken=USD)")
            sp.add_argument("--top-volume", type=int, default=30, help="กรองเอา N เหรียญ volume สูงสุด")
            sp.add_argument("--top-momentum", type=int, default=5, help="โชว์ N เหรียญ momentum แรงสุด")
            sp.add_argument("--lookback", type=int, default=30, help="วัด momentum ย้อนหลังกี่แท่ง")
            sp.add_argument("--min-volume", type=float, default=500_000,
                            help="volume 24h ขั้นต่ำ (quote) — กันเหรียญสภาพคล่องต่ำ/ปั่น")
            sp.add_argument("--notify", action="store_true", help="ส่งผลเข้า Discord ด้วย")
            sp.add_argument("--every-hours", type=float, default=0,
                            help="ทำเฉพาะถ้าผ่านมาแล้ว N ชม.ตั้งแต่ครั้งก่อน (0 = ทำทันที)")
        if name == "bot":
            sp.add_argument("--once", action="store_true", help="รันรอบเดียวแล้วหยุด")
        if name == "montecarlo":
            sp.add_argument("--runs", type=int, default=1000, help="จำนวนรอบจำลอง")
        if name in ("report", "validate"):
            sp.add_argument("--every-hours", type=float, default=0,
                            help="ทำเฉพาะถ้าผ่านมาแล้ว N ชม.ตั้งแต่ครั้งก่อน (0 = ทำทันที)")
        if name == "validate":
            sp.add_argument("--mc-runs", type=int, default=500, help="จำนวนรอบ Monte Carlo")
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

    elif args.command == "montecarlo":
        from .montecarlo import monte_carlo, summarize
        ex = make_exchange(cfg)
        df = fetch_ohlcv(ex, args.symbol, args.timeframe, args.limit)
        print(f"\n🎲 Monte Carlo | {args.symbol} {args.timeframe} | กลยุทธ์={cfg['strategy']['name']}")
        stats = monte_carlo(df, cfg, args.timeframe, runs=args.runs)
        print("\n" + summarize(stats))

    elif args.command == "report":
        from datetime import datetime, timezone

        from . import alerts, state
        from .report import build_report
        initial = float(cfg.get("paper", {}).get("initial_cash", 300))
        if args.every_hours > 0:
            last = state.get_marker("report")
            if last:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
                if elapsed < args.every_hours:
                    print(f"ยังไม่ถึงรอบส่ง report (ผ่าน {elapsed:.1f}/{args.every_hours:.0f} ชม.)")
                    return 0
        portfolio = state.load_portfolio(initial)
        journal_path = cfg.get("risk", {}).get("journal_path", "trade_journal.csv")
        alerts.notify(cfg, build_report(journal_path, portfolio, initial))
        if args.every_hours > 0:
            state.set_marker("report")

    elif args.command == "scan":
        from .scanner import format_scan, scan_market
        if args.every_hours > 0:
            from datetime import datetime, timezone

            from . import state
            last = state.get_marker("scan")
            if last:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
                if elapsed < args.every_hours:
                    print(f"ยังไม่ถึงรอบ scan (ผ่าน {elapsed:.1f}/{args.every_hours:.0f} ชม.)")
                    return 0
        ex = make_exchange(cfg)
        rows, n_liquid = scan_market(
            ex, quote=args.quote, top_by_volume=args.top_volume,
            top_by_momentum=args.top_momentum, timeframe=args.timeframe,
            lookback=args.lookback, min_volume=args.min_volume,
        )
        msg = format_scan(rows, n_liquid, args.timeframe, args.lookback)
        print(msg)
        if args.notify:
            from . import alerts
            alerts.notify(cfg, msg)
        if args.every_hours > 0:
            from . import state
            state.set_marker("scan")

    elif args.command == "pbo":
        from .pbo import build_matrix, cscv_pbo, summarize
        ex = make_exchange(cfg)
        df = fetch_ohlcv(ex, args.symbol, args.timeframe, args.limit)
        print(f"\n🔬 PBO/CSCV | {args.symbol} {args.timeframe} | mode={args.mode}")
        matrix, labels = build_matrix(df, cfg, args.timeframe, mode=args.mode)
        stats = cscv_pbo(matrix, blocks=args.blocks)
        print("\n" + summarize(stats, labels, args.timeframe))
        print("⚠️  read-only — ไม่กระทบการเทรด")

    elif args.command == "status":
        from datetime import datetime, timezone

        from . import alerts, state
        from .status import build_status
        if args.every_hours > 0:
            last = state.get_marker("status")
            if last:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
                if elapsed < args.every_hours:
                    print(f"ยังไม่ถึงรอบ status (ผ่าน {elapsed:.1f}/{args.every_hours:.0f} ชม.)")
                    return 0
        ex = make_exchange(cfg)
        msg = build_status(ex, cfg, args.timeframe)
        print(msg)
        if args.notify:
            alerts.notify(cfg, msg)
        if args.every_hours > 0:
            state.set_marker("status")

    elif args.command == "validate":
        from datetime import datetime, timezone

        from . import alerts, state
        from .validate import run_validation
        if args.every_hours > 0:
            last = state.get_marker("validate")
            if last:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
                if elapsed < args.every_hours:
                    print(f"ยังไม่ถึงรอบ validate (ผ่าน {elapsed:.1f}/{args.every_hours:.0f} ชม.)")
                    return 0
        symbols = cfg.get("bot", {}).get("symbols") or [args.symbol]
        ex = make_exchange(cfg)
        msg, _degraded = run_validation(ex, cfg, symbols, args.timeframe,
                                        limit=args.limit, mc_runs=args.mc_runs)
        alerts.notify(cfg, msg)
        if args.every_hours > 0:
            state.set_marker("validate")

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

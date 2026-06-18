# สรุปความรู้จากเอกสาร → นำมาปรับ logic บอท

สรุปสิ่งที่อ่านจากหนังสือ/เปเปอร์ และแมปว่าแต่ละอันถูกนำมาใช้ในโค้ดตรงไหน
(ปรับปรุงล่าสุด: 2026-06-18)

> **แก่นที่ทุกเล่มพูดตรงกัน:** *Risk management สำคัญกว่าจุดเข้า* — การจัดการเงิน/ความเสี่ยง
> และวินัย คือสิ่งที่แยก "ผู้รอด" ออกจาก "ผู้เจ๊ง" ไม่ใช่การเดาตลาดเก่ง

---

## 1. My_Learnings.pdf — "9 Profitable Trading Strategies" (HumbleTraders / Roman Sadowski)

หนังสือ forex day-trading ระดับเริ่มต้น/การตลาด เน้นกลยุทธ์ + money management

**กลยุทธ์ในเล่ม:** Momentum Reversal, MA crossover (20/60/100), Heikin-Ashi + Stochastic,
Swing, Candlestick patterns (engulfing/hammer), Role Reversal (S/R), Bollinger squeeze,
Narrow Range, 2-period RSI

**Money management (ส่วนที่มีค่าสุด):**
- ย้าย SL มา breakeven หลังกำไร (50 pips) → **ทำแล้ว: `breakeven_trigger_pct`**
- "Let your winners run" / trail stop → **ทำแล้ว: `trailing_stop_pct`**
- กฎ 3% ห้ามเสี่ยงเกินต่อไม้ → **ทำแล้ว: risk-based sizing 1%/ไม้**
- ใช้กลยุทธ์ที่ทดสอบแล้ว ไม่เทรดมั่ว → **ทำแล้ว: backtest + walk-forward**

**นำมาใช้:** เพิ่มกลยุทธ์ `bb_squeeze`, `rsi2` (Connors), `heikin_stoch` + indicator
`stochastic`, `heikin_ashi`
→ **backtest 5 เหรียญพบ rsi2 ชนะขาดลอย (drawdown ต่ำสุด) → ตั้งเป็น default**
→ heikin_stoch แย่กว่า buy&hold (ไม่ใช้)

**ไม่เอา:** S/R zones, Role reversal, candlestick ตีความเอง, fundamental/COT, เลี่ยงข่าว
— ต้องใช้ดุลพินิจคน อัตโนมัติไม่ได้แบบทนทาน

---

## 2. position_sizing.pdf — Ginyard (2001), Uppsala University (master thesis)

งานวิจัยจิตวิทยา/ทดลอง: ให้ 2 กลุ่มเทรดเงินจำลอง วัดผลของ "ขนาดไม้"

**ผลการทดลอง (มีนัยสำคัญทางสถิติ):**
- กลุ่มที่เรียน position sizing **เจ๊งน้อยกว่า 10 เท่า** (p < .01)
- ผู้ที่กำไรระยะยาว **ลงไม้เล็กกว่า** ผู้ที่เจ๊ง (p < .0001)
- คณิตศาสตร์ drawdown: ขาดทุน 50% ต้องกำไร 100% ถึงคืนทุน → **cut losses short**
- กับดักจิตวิทยา: loss aversion, disposition effect (ขายตัวกำไรเร็ว เก็บตัวขาดทุน), sunk cost

**แมปกับบอท:** ยืนยันแนวทาง **risk-based sizing 1%/ไม้** (อนุรักษ์นิยม = อยู่รอด)
+ **circuit breaker** หยุดเมื่อขาดทุนหนัก (กัน "throwing good money after bad")
+ บอทไม่มีอารมณ์ → ตัด disposition effect/loss aversion ออกโดยอัตโนมัติ

---

## 3. WB_1647_Waverly_Position_Sizing.pdf — Waverly Advisors (2013)

สไลด์มืออาชีพ: position sizing + trade management (ปฏิบัติจริงมาก)

**Position sizing plans:** Fixed Units, Equal Dollar, **Equal Risk (2-4%)**,
Equal Volatility (ATR), Kelly, Optimal f
- Equal Risk = ของเรา (เราใช้ 1% อนุรักษ์กว่าที่แนะนำ 2-4%) → **ทำแล้ว**
- หลักการ: "ไม่มีไม้เดียวที่ทำให้ออกจากเกมได้" + "ลดความเสียหายจากการแพ้ติดกัน"

**Trade management (ของใหม่):**
- **Initial stop**: fixed % / volatility-adjusted (ATR) / market structure → **ทำแล้ว: ATR stops**
- **Partial profits**: ขายบางส่วนที่ 1R, 2R (25-33% ต่อจุด) ถือที่เหลือวิ่งต่อ → ⏸️ *ยังไม่ทำ*
- **Time stop**: ออกถ้าถือนานเกินไม่ไปไหน → ⏸️ *ยังไม่ทำ*
- **R-multiples**: วัดเป้าเป็นเท่าของความเสี่ยงเริ่มต้น (R)
- ข้อคิด: *"random entries ก็กำไรได้"* → trade management สำคัญกว่าจุดเข้า

---

## 4. Quantitative_Trading_Strategy_Backtesting_and_Perf.pdf — Poudel & Paudel (2025), QJMSS

เปเปอร์วิชาการ: สร้าง quant strategy บน NEPSE (ตลาดหุ้นเนปาล) ด้วย Python

**กรอบงานคล้ายของเรามาก:** long-only, rule-based, Z-score + RSI + MA 240 วัน,
dynamic position sizing by risk, trade limits, cooldown → เน้น capital preservation

**เมตริกประเมิน:** CAGR, Sharpe, **Sortino**, Max Drawdown, Win Rate,
**Profit Factor**, **Recovery Factor** + **Monte Carlo** + sensitivity analysis

**นำมาใช้:**
- เพิ่มเมตริก **Sortino / Profit Factor / Recovery Factor** ใน backtest → **ทำแล้ว**
- เพิ่มคำสั่ง **`montecarlo`** จำลองสุ่มลำดับผล → ดู "กรณีแย่ (P05)" + โอกาสขาดทุน → **ทำแล้ว**

**ผลสรุปเปเปอร์:** ระบบ rule-based ง่าย ๆ ชนะ buy&hold ด้าน risk-adjusted + drawdown ต่ำกว่า
แม้ในตลาดที่ไม่มีประสิทธิภาพ → ตรงกับเป้าหมายบอทเรา

---

## 5. ssrn-2602320.pdf — Foltice & Langer, "Profitable Momentum Strategies for Individual Investors"

เปเปอร์ SSRN: momentum สำหรับนักลงทุนรายย่อย (ข้อมูล NYSE 1991-2010)

**ประเด็นหลัก:**
- momentum ดั้งเดิม (Jegadeesh-Titman) ต้อง short + ซื้อหลายร้อยตัว → รายย่อยทำไม่ได้/ค่าธรรมเนียมกิน
- **เวอร์ชันที่ใช้ได้จริง: long-only ซื้อเฉพาะตัว "ผู้ชนะ" ไม่กี่ตัว**
- **momentum profit เพิ่มขึ้นเมื่อจำนวนหุ้นลดลง** (เน้นตัวแข็งสุด)
- ค่าธรรมเนียมสำคัญ → ความถี่ rebalance ที่เหมาะ = รายเดือน-ราย 6 เดือน
- ใช้ทุนเริ่มต้น ≥ $5,000 ถึงคุ้ม

**นำมาใช้:** เพิ่ม **relative momentum filter** (`momentum_filter`, default OFF)
ซื้อเฉพาะเหรียญที่ momentum แรงสุด top-K ในตะกร้า `bot.symbols` → **ทำแล้ว**

---

## 6. Momentum_Trading_for_the_Private_Investor.pdf (497 หน้า — หนังสือเต็มเล่ม)

ตำรา momentum/relative-strength เชิงวิชาการ (อ่านเฉพาะส่วนกฎที่ใช้ได้)

**กฎ relative strength แบบคลาสสิก:**
- จัดอันดับสินทรัพย์ด้วย "ผลตอบแทนช่วง ranking period" (1-12 เดือน)
- ถือตัวอันดับสูงสุดใน "holding period" ถัดมา
- ข้าม 1 ช่วงระหว่าง ranking กับ holding (skip-a-period)
- คอมโบที่ดีสุด: ranking/holding = 6, 9, 12 เดือน

**แมปกับบอท:** สนับสนุน `momentum_filter` เดียวกับ SSRN (จัดอันดับด้วยผลตอบแทนย้อนหลัง
แล้วถือตัวแข็งสุด) — เราย่อ lookback ให้เข้ากับ timeframe คริปโต (เร็วกว่าหุ้น)

---

## สรุปตาราง: ความรู้ → ฟีเจอร์ในบอท

| ความรู้ | เอกสาร | สถานะในบอท |
|---------|--------|------------|
| Risk-based sizing (เสี่ยงคงที่/ไม้) | Ginyard, Waverly | ✅ `paper.sizing_mode=risk` 1% |
| Cut losses / stop loss | ทุกเล่ม | ✅ SL + ATR stops |
| Breakeven stop | HumbleTraders | ✅ `breakeven_trigger_pct` |
| Trailing stop (let winners run) | HumbleTraders, Waverly | ✅ `trailing_stop_pct` |
| จำกัดความเสี่ยงพอร์ตรวม | Ginyard, Waverly | ✅ `max_concurrent_positions` |
| Circuit breaker | Ginyard (sunk cost) | ✅ `circuit_breaker_pct` |
| Sortino/Profit Factor/Recovery | Poudel | ✅ backtest summary |
| Monte Carlo | Poudel | ✅ `montecarlo` command |
| Walk-forward (กัน overfit) | (best practice) | ✅ `walkforward` command |
| Relative momentum (ซื้อตัวแข็งสุด) | SSRN, Momentum book | ✅ `momentum_filter` (default OFF) |
| กลยุทธ์ rsi2/bb_squeeze/heikin_stoch | HumbleTraders | ✅ เพิ่มแล้ว (rsi2 = default) |
| Partial take-profit (scale out 1R/2R) | Waverly | ⏸️ ยังไม่ทำ |
| Time stop | Waverly | ⏸️ ยังไม่ทำ |
| Time-series momentum (tsmom) | Levy, Martin, MomTransformer | ✅ strategy `tsmom` |
| Volatility targeting sizing | Levy, MomTransformer | ✅ `sizing_mode=volatility` |
| Deep-learning momentum (Transformer) | MomTransformer | ❌ ไม่ทำ (infra ไม่ไหว) |

---

## 7. Trend-Following via Dynamic Momentum Learning — Levy & Lopes (2021), Insper

**แก่น:** Time-Series Momentum (TSMOM) = ถือตามเทรนด์ของสินทรัพย์ "ตัวมันเอง"
(long ถ้าผลตอบแทนย้อนหลังเป็นบวก) ต่างจาก cross-sectional (เทียบกับเพื่อน)
- เสนอ dynamic classifier เรียนรู้ว่า lookback ไหนสำคัญ + สลับ "ความเร็ว" momentum ตอน turning point
- **Volatility targeting:** ปรับ position ให้ความผันผวนต่อปีคงที่ σ_tgt = 40% (Moskowitz et al. 2012)
- รวมหลาย lookback (เร็ว+ช้า) ทนทานกว่าใช้ค่าเดียว

**นำมาใช้:** strategy `tsmom` (รวมหลาย lookback โหวต) + `sizing_mode: volatility` (target_vol 40%)

## 8. Momentum Transformer — Wood, Giegerich, Roberts, Zohren (Oxford-Man Institute)

**แก่น:** Deep learning (attention/LSTM hybrid) สำหรับ TSMOM optimize Sharpe ตรง ๆ
- ปรับตัวตาม regime ใหม่ (เช่น วิกฤต COVID), จับหลาย timescale พร้อมกันผ่าน multi-head attention
- คิดผลตอบแทน net of transaction cost, interpretable (ดูได้ว่าปัจจัยไหนสำคัญตอนไหน)

**ตัดสินใจ: ❌ ไม่ implement โมเดล** — ต้อง train, PyTorch/GPU, ข้อมูลมหาศาล, retrain เรื่อย ๆ
e2-micro (RAM 1GB) ไม่ไหว + เสี่ยง overfit สูง + ดูแลยาก
→ **ยืมแนวคิดแทน:** vol targeting + multi-lookback (แทน multi-timescale)

## 9. Design and Analysis of Momentum Trading Strategies — Richard J. Martin (2023), Imperial College

**แก่น (คณิตศาสตร์):** วิเคราะห์ skewness ของกลยุทธ์ momentum
- สัญญาณ momentum = normalized EMA filter (+ nonlinear transform)
- **ผลตอบแทน momentum เบ้บวก (positively skewed) โดยธรรมชาติ** — เพราะถือไม้ใหญ่ตอนกำไรแล้ว
  (= cut losses, let winners run) ตรงข้ามกับ mean-reversion ที่เบ้ลบ
- positive skew เกิดจาก "การออกแบบกลยุทธ์" ไม่ใช่จากตลาด → จริงแม้ตลาดไม่มี serial correlation

**นำมาใช้:** ยืนยันทางคณิตศาสตร์ว่า **trailing stop / let-winners-run ที่เรามี ถูกหลักการ**
(default OFF — เปิดในตลาดขาขึ้น) + tsmom เป็น trend-following ที่ให้ skew บวก

---

## ผลทดสอบ tsmom (ข้อมูลขาลง 2026)

backtest 5 เหรียญ: **tsmom รวม -29.2% แย่กว่า rsi2 -2.1%** มาก
- เหตุ: tsmom เป็น trend-following → ในตลาด"ขาลง"โดน whipsaw (ไล่ซื้อ false breakout)
- พลังจริงของ tsmom อยู่ใน**ขาขึ้นยาว ๆ** ซึ่งข้อมูลนี้ไม่มี
- → คง **rsi2 เป็น default** (mean-reversion เหมาะตลาดปัจจุบัน), tsmom เปิดใช้ได้ตอนตลาด trend ชัด

> **บทเรียนที่ย้ำตัวเอง:** "ฉลาดขึ้น" ≠ "เพิ่มอินดิเคเตอร์" — แต่คือ *จัดการความเสี่ยงให้ดี
> + พิสูจน์ด้วย data (backtest/walk-forward/Monte Carlo) ก่อนเชื่อ* ไม่ใช่เชื่อหนังสือ

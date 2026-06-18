# สรุปความรู้ ชุดที่ 2 — Overfitting, Execution, Risk Parity

7 เปเปอร์เชิงลึก (ครอบคลุม: การพิสูจน์ backtest, ต้นทุนการเทรดจริง, การกระจายความเสี่ยง)
อ่าน + ดูรูปประกอบแล้ว · ปรับปรุง 2026-06-19

> **แก่นรวม:** เครื่องมือเราพร้อมแล้ว แต่ 3 เปเปอร์แรกเตือนว่า **"เอดจ์ที่เห็นใน backtest อาจหลอก"**
> และ 4 เปเปอร์หลังเตือนว่า **"ผลจริงจะแย่กว่า backtest เพราะต้นทุน + การกระจุกความเสี่ยง"**

---

## กลุ่ม A — Backtest Overfitting (อันตรายที่ซ่อนอยู่)

### 1. Pseudo-Mathematics and Financial Charlatanism — Bailey, Borwein, López de Prado, Zhu (2014)
**แก่น:** ถ้าลองพารามิเตอร์หลายชุด (N trials) เดี๋ยวก็เจอ backtest สวยเสมอ **แม้กลยุทธ์ไม่มีเอดจ์จริง**
- **MinBTL (Minimum Backtest Length):** ยิ่งลองหลายชุด ยิ่งต้องมีข้อมูลยาวขึ้น/ตั้งเกณฑ์ Sharpe สูงขึ้น ไม่งั้นที่เจอคือ noise
- **ถ้า return มี "memory" (serial correlation) → overfit ทำให้ OOS ติดลบ** (ยิ่ง optimize IS เก่ง ยิ่งเจ๊ง OOS — รูป scatter IS↔OOS ลาดลงเป็นลบ)
- คำคม von Neumann: *"4 พารามิเตอร์วาดช้างได้ 5 ตัวทำให้ช้างกระดิกงวงได้"*
- **ปักธง:** backtest ที่ไม่บอกว่าลองมากี่ชุด = เชื่อไม่ได้

**→ ใช้กับบอทเรา:** `optimize` (grid search) + `walkforward` ลองหลายชุด = เสี่ยง overfit
ควร (1) ใช้พารามิเตอร์น้อย (2) ตั้งเกณฑ์ Sharpe สูงขึ้นถ้าลองเยอะ (3) ไม่หลงค่าที่ "ดีสุดในอดีต"

### 2. The Probability of Backtest Overfitting (PBO) — ทีมเดียวกัน (2015)
**แก่น:** เสนอวิธีวัด **PBO = ความน่าจะเป็นที่กลยุทธ์ "ดีสุดในอดีต" จะหลุดต่ำกว่าค่ากลางในอนาคต**
- วิธี **CSCV** (Combinatorially Symmetric Cross-Validation): หั่นข้อมูลเป็นบล็อก สลับ IS/OOS ทุกชุดที่เป็นไปได้ แล้วดูว่ากลยุทธ์ที่ชนะ IS ยังติดอันดับบน OOS ไหม
- hold-out ธรรมดา **ไม่น่าเชื่อถือ** สำหรับ backtest → CSCV ดีกว่า
- PBO สูง = กระบวนการเลือกกลยุทธ์ของคุณกำลัง overfit

**→ ใช้กับบอทเรา:** คำสั่ง `validate` (เทียบ recent vs earlier) คือเวอร์ชันย่อของแนวคิดนี้ — ถ้าอยากจริงจังทำ CSCV เต็มได้

### 3. Mathematical Appendices to PBO — ทีมเดียวกัน
**แก่น:** ภาคผนวกสูตร + test cases ของ PBO (full/high/low overfit) สำหรับ implement CSCV
**→ ใช้กับบอทเรา:** เป็นคู่มืออ้างอิงถ้าจะเขียน PBO/CSCV ในอนาคต

### 4. How should you discount your backtest PnL? — Rej, Seager, Bouchaud (CFM, 2018)
**แก่น:** เสนอ **"discount factor"** สำหรับหั่น Sharpe ของ backtest ลง เพราะส่วนหนึ่งคือ noise ที่บังเอิญเข้าทาง
- Sharpe ที่วัดได้ = Sharpe จริง + noise → ต้อง**หักส่วน noise ออก**ก่อนเชื่อ
- **residual Sharpe ที่เหลือควร ~0.3-0.5 ขึ้นไป** ถึงน่าเอาไปใช้จริง
- ยิ่งกรองทิ้งกลยุทธ์แพ้ใน IS เยอะ ยิ่ง overfit

**→ ใช้กับบอทเรา (สำคัญมาก):** rsi2 ของเรา backtest ได้ **PF ~1.0 / Sharpe เตี้ย** → พอหัก discount แล้ว **เอดจ์เหลือเกือบศูนย์** = ตรงกับที่บอกว่า "อย่าคาดหวังกำไร" นี่คือหลักฐานเชิงตัวเลข

---

## กลุ่ม B — Execution & Microstructure (ทำไมผลจริง < backtest)

### 5. Optimal Execution of Portfolio Transactions — Almgren & Chriss (2000)
**แก่น:** การส่งออเดอร์ใหญ่มี **trade-off**: เทรดเร็ว = โดน market impact แพง / เทรดช้า = เสี่ยงราคาวิ่งหนี
- แยก impact เป็น **permanent** (ดันราคาถาวร) + **temporary** (ชั่วคราวตอนกดออเดอร์)
- มี **efficient frontier** ของกลยุทธ์ทยอยขาย — เลือกจุดสมดุลตามความกลัวเสี่ยง
- ออเดอร์ใหญ่ควร**ทยอย** ไม่ใช่ market order ทีเดียว

**→ ใช้กับบอทเรา:** backtest เราสมมติฟิลที่ราคาปิด + fee คงที่ = **มองโลกสวย** ของจริงมี slippage → **ผลจริงจะแย่กว่า backtest** โดยเฉพาะเหรียญสภาพคล่องต่ำ ตอนเงินจริงควรใช้ limit order/ทยอย

### 6. Limit Order Books — Gould, Porter, et al. (Oxford, 2013)
**แก่น:** survey กลไก LOB (สมุดคำสั่งซื้อขาย) + stylized facts: spread, depth, order flow
- ต้นทุนซ่อนของการเทรด = ข้าม spread + กิน depth (ยิ่งออเดอร์ใหญ่ ยิ่งกินลึก = ราคาแย่)
- สภาพคล่องต่ำ = spread กว้าง = ต้นทุนสูง

**→ ใช้กับบอทเรา:** ยืนยันว่า **min-volume filter ใน `scan` ถูกทาง** (เลี่ยงเหรียญ spread กว้าง) และตอนเทรดจริงต้องระวัง spread ของเหรียญเล็ก

---

## กลุ่ม C — Portfolio Risk (การกระจายความเสี่ยงจริง)

### 7. Understanding Risk Parity — AQR (Hurst, Johnson, Ooi, 2010)
**แก่น:** กระจายความเสี่ยงต้องดู **"สัดส่วนความเสี่ยง" ไม่ใช่ "สัดส่วนเงิน"**
- พอร์ต 60/40 (หุ้น/บอนด์) จริง ๆ คือ **~90% ความเสี่ยงมาจากหุ้น** (รูป Exhibit 3) → ไม่กระจายจริง!
- **Risk Parity:** จัดให้แต่ละสินทรัพย์มี **risk contribution เท่ากัน** (ลงน้อยในของผันผวนสูง) → ทนทานทุกสภาพตลาด
- ขนาด ∝ 1/ความผันผวน (inverse-vol)

**→ ใช้กับบอทเรา:** ตะกร้า BTC/ETH/SOL/XRP/ADA **วิ่งทางเดียวกันหมด (correlation สูง)** → ถือ 5 ตัว ≠ กระจาย แต่คือ "เดิมพันก้อนเดียว 5 เท่า"
- ข่าวดี: เรามี **`sizing_mode=volatility`** (vol-targeting) แล้ว = ลงน้อยในเหรียญผันผวนสูง ตรงหลัก risk parity
- ที่ขาด: ยังไม่ดู **correlation** ระหว่างเหรียญ (ควรจำกัดถือเหรียญที่สัมพันธ์กันสูงพร้อมกัน)

---

## 🎯 สรุปสิ่งที่ควรปรับในบอท (เรียงตามคุ้ม)
| ปรับอะไร | จากเปเปอร์ | สถานะ |
|---------|-----------|-------|
| **ใส่ slippage/cost จริงใน backtest** | Almgren, LOB | ⏳ ควรทำก่อนเงินจริง |
| **นับ N trials + หั่น Sharpe (discount/MinBTL)** ใน optimize/walkforward | #1,#4 | ⏳ เตือน overfit |
| **correlation-aware** (ไม่ถือเหรียญสัมพันธ์สูงพร้อมกัน) | Risk Parity | ⏳ |
| vol-targeting sizing | Risk Parity | ✅ มีแล้ว |
| min-volume filter (เลี่ยง spread กว้าง) | LOB | ✅ มีแล้ว |
| recent-vs-earlier validation | PBO | ✅ มีแล้ว (ย่อ) |

> **บทเรียนใหญ่สุด:** เปเปอร์ overfitting (กลุ่ม A) ยืนยันสิ่งที่เราเห็น — rsi2 เอดจ์บาง (PF~1) พอหัก noise/cost
> แล้ว **อาจไม่เหลือกำไรจริง** → ยิ่งต้องเริ่ม paper/Testnet ก่อนเสมอ และอย่าทุ่มเงินจริงเพราะ backtest สวย

---

## 🧪 ผลทดสอบจริงหลังใส่ slippage (2026-06-19)
เพิ่ม `slippage_pct=0.0005` (5bps/ด้าน) เข้า backtest + paper sim ตาม Almgren/LOB แล้วทดสอบ rsi2 บน 5 เหรียญ /USD:

| | fee เดิม (ไม่คิด slippage) | +slippage จริง |
|---|:---:|:---:|
| ผลรวม 5 เหรียญ | -5.58% | **-9.72%** |
| เหรียญที่เป็นบวก | BTC/SOL/XRP (เฉียด 0) | **ไม่มีเลย — ติดลบหมด** |

**สรุปเชิงประจักษ์:** เอดจ์เล็ก ๆ ของ rsi2 **หายเกลี้ยงพอคิดต้นทุนจริง** → ยืนยันคำเตือนของเปเปอร์ Bouchaud/LdP
ว่าเอดจ์ในกระดาษคือภาพลวง mean-reversion ที่เทรดบ่อยอ่อนไหวต่อต้นทุนมาก
→ **ในตลาดขาลงนี้ rsi2 ไม่มีเอดจ์จริง** การที่บอทไม่เทรด (market_filter บล็อก) คือสิ่งที่ถูกต้อง

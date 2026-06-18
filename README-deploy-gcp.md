# Deploy crypto-trader บน Google Cloud (ฟรีถาวร — แนะนำแทน Oracle)

GCP `e2-micro` **Always Free** สร้างได้ทันที **ไม่ต้องลุ้น capacity** เหมือน Oracle
และรันค้างเป็น process 24/7 จริง (timing แม่นกว่า GitHub Actions ที่ cron ดีเลย์/ข้าม)

| ไฟล์ | หน้าที่ |
|------|---------|
| `deploy-gcp.sh` | สร้าง VM ฟรี + แนบ startup-script (รันบน Mac ครั้งเดียว) |
| `deploy-cloudinit.sh` | สคริปต์ที่ VM รันเองตอนบูต: ลง deps + clone repo + venv + systemd |

> เงื่อนไข Always Free: ต้องเป็น **e2-micro**, โซน **us-west1 / us-central1 / us-east1**,
> standard persistent disk **≤30GB**, 1 ตัว/เดือน เท่านั้น (สคริปต์ตั้งค่าให้ครบแล้ว)

---

## เตรียมครั้งเดียว (บน Mac)

1. **สมัคร Google Cloud** + ผูกบัตร (ใช้ฟรีทเทียร์ ไม่โดนคิดเงินถ้าอยู่ใน e2-micro)
   เปิด https://console.cloud.google.com แล้วสร้าง **Project** ใหม่ (จด PROJECT_ID ไว้)

2. **ติดตั้ง gcloud CLI**: https://cloud.google.com/sdk/docs/install
   (macOS: `brew install --cask google-cloud-sdk`)

3. **ล็อกอิน + เลือก project**:
   ```bash
   gcloud auth login
   gcloud config set project <PROJECT_ID>
   ```

4. **มี SSH key แล้วหรือยัง** (ถ้าไม่มี): `ssh-keygen -t rsa -b 4096` → ได้ `~/.ssh/id_rsa.pub`

---

## รันดีพลอย (คำสั่งเดียว)

```bash
cd ~/Downloads/trade
./deploy-gcp.sh
```

- สร้าง VM ทันที (ไม่ต้องวน retry) แล้วพิมพ์ **PUBLIC IP** ให้
- startup-script จะลง deps + clone repo + ตั้ง systemd ให้เองในพื้นหลัง (~2-3 นาที)

> อยากเปลี่ยนโซน/ชื่อ VM: แก้ตัวแปรหัวไฟล์ `deploy-gcp.sh` (`ZONE`, `NAME`)

---

## หลัง VM ขึ้นแล้ว (ทำครั้งเดียว)

รอ ~3 นาทีให้ดีพลอยเสร็จ แล้ว:

```bash
ssh -i ~/.ssh/id_rsa ubuntu@<PUBLIC_IP>

cat ~/NEXT_STEPS.txt                         # ดูขั้นต่อไป
sudo tail -n 50 /var/log/deploy-bot.log      # ดู log การดีพลอย

nano ~/crypto-trader/config.yaml             # ใส่ Testnet api_key/api_secret + sandbox: true

cd ~/crypto-trader && .venv/bin/python -m crypto_trader bot BTC/USDT -t 1h --once   # ทดสอบ
sudo systemctl start crypto-trader           # สตาร์ทรันค้าง 24/7
journalctl -u crypto-trader -f               # ดู log สด
```

---

## หมายเหตุความปลอดภัย (เหมือนเดิม)

- **ไม่ฝัง API key ใน startup-script/metadata** — metadata อ่านได้จากใน VM และคอนโซล
  ใส่ key ตรง ๆ บน VM ใน `config.yaml` เท่านั้น
- ตอนใช้เงินจริง: API key เปิดเฉพาะ **Spot Trading ห้ามเปิด Withdraw**
- เริ่ม **Testnet (sandbox: true)** เสมอ → ค่อยขยับเป็นเงินก้อนเล็ก

---

## ลบทิ้ง (ถ้าไม่ใช้แล้ว กันโดนคิดเงิน)

```bash
gcloud compute instances delete crypto-bot --zone us-central1-a
```

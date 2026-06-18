# Deploy crypto-trader บน OCI ฟรีทเทียร์ (อัตโนมัติ)

2 สคริปต์ทำงานคู่กัน เพื่อ **สร้าง instance ฟรีให้สำเร็จ** (สู้ปัญหา out-of-capacity)
แล้ว **ดีพลอยบอทให้อัตโนมัติตอนบูตครั้งแรก**

| ไฟล์ | หน้าที่ |
|------|---------|
| `retry-launch.sh` | วนสั่งสร้าง instance ฟรีจน capacity ว่าง (รันบนเครื่อง Mac ของคุณ) |
| `deploy-cloudinit.sh` | cloud-init ที่ VM รันเองตอนบูต: ลง deps + clone repo + venv + systemd |

> อยู่กับ **Always Free** ล้วน ไม่ต้องอัปเกรด PAYG · เริ่มที่ **Testnet** เสมอ

---

## เตรียมครั้งเดียว (บน Mac)

1. **ติดตั้ง OCI CLI** แล้วตั้งค่า:
   ```bash
   bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)"
   oci setup config        # ใส่ tenancy/user OCID + region (สิงคโปร์) + สร้าง API key
   ```
   (ต้องเอา public key ที่ `oci setup config` สร้าง ไปใส่ใน Console → User Settings → API Keys ด้วย)

2. **เตรียม SSH key** (ถ้ายังไม่มี): `ssh-keygen -t rsa -b 4096`  → จะได้ `~/.ssh/id_rsa.pub`

3. **หา OCID 2 ตัวที่ต้องกรอก** ใน `retry-launch.sh`:
   - `COMPARTMENT_OCID` → Console → เมนู → Identity → Compartments (หรือใช้ tenancy OCID ก็ได้)
   - `SUBNET_OCID` → Console → Networking → VCN ที่สร้างไว้ → Subnets → คัดลอก OCID ของ public subnet

---

## รันดีพลอย

วางสองไฟล์ไว้โฟลเดอร์เดียวกัน แล้ว:

```bash
chmod +x retry-launch.sh deploy-cloudinit.sh
# แก้ค่า 3 ตัวบนหัวไฟล์ retry-launch.sh ให้เรียบร้อยก่อน
./retry-launch.sh
```

- ถ้าเต็ม จะขึ้น "ยังเต็ม... รออีก 60s" แล้ววนเอง — ปล่อยทิ้งไว้จนได้
- พอสำเร็จจะพิมพ์ `INSTANCE_ID` และคำสั่งดู public IP ให้
- **อยากได้สเปคดีกว่า**: เปลี่ยน `SHAPE="VM.Standard.A1.Flex"` (ARM, ฟรีเหมือนกัน) บางทีว่างกว่า E2

---

## หลัง instance ขึ้นแล้ว (cloud-init ดีพลอยให้อัตโนมัติ)

cloud-init จะลงทุกอย่างให้เอง เหลือแค่ใส่ key + สตาร์ท (ทำครั้งเดียว):

```bash
# SSH เข้า VM (เอา IP จากคำสั่งที่สคริปต์พิมพ์ให้)
ssh -i ~/.ssh/id_rsa ubuntu@<PUBLIC_IP>

# เช็คว่า cloud-init ดีพลอยเสร็จ (ดูโน้ตขั้นต่อไป)
cat ~/NEXT_STEPS.txt
sudo tail -n 50 /var/log/deploy-bot.log     # ดู log การดีพลอย

# ใส่ Testnet API key
nano ~/crypto-trader/config.yaml            # api_key/api_secret + sandbox: true

# ทดสอบ แล้วสตาร์ทรันค้าง 24/7
cd ~/crypto-trader && .venv/bin/python -m crypto_trader bot BTC/USDT -t 1h --once
sudo systemctl start crypto-trader
journalctl -u crypto-trader -f              # ดู log สด
```

---

## หมายเหตุความปลอดภัย

- **ไม่ฝัง API key ใน cloud-init/สคริปต์** เพราะ user-data อ่านได้จากใน VM และจากคอนโซล —
  ใส่ key ตรง ๆ บน VM ใน `config.yaml` เท่านั้น
- ตอนใช้เงินจริง: API key ของ Binance ให้เปิดเฉพาะ **Spot Trading ห้ามเปิด Withdraw**
- การเทรดอัตโนมัติด้วยเงินจริงมีความเสี่ยงขาดทุน เริ่ม Testnet → เงินก้อนเล็กเสมอ

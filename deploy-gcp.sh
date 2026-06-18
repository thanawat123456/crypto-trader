#!/usr/bin/env bash
# ============================================================================
# deploy-gcp.sh — สร้าง VM "ฟรีถาวร" บน Google Cloud (e2-micro Always Free)
#   + แนบ startup-script (deploy-cloudinit.sh) ให้ดีพลอยบอทอัตโนมัติตอนบูตครั้งแรก
#
# ต่างจาก Oracle: GCP แทบไม่เคย out-of-capacity → ไม่ต้องมี retry loop
#
# ต้องมีก่อน (ทำครั้งเดียว):
#   1) ติดตั้ง gcloud CLI:  https://cloud.google.com/sdk/docs/install
#   2) gcloud auth login            # ล็อกอินบัญชี Google
#   3) gcloud config set project <PROJECT_ID>
#   4) gcloud services enable compute.googleapis.com
# ============================================================================
set -euo pipefail

# ====== ปรับค่าได้ (ค่า default ใช้ได้เลย) ======
PROJECT="${GCP_PROJECT:-}"          # ว่าง = ใช้ project ปัจจุบันของ gcloud
ZONE="us-central1-a"                # Always Free เฉพาะ us-west1 / us-central1 / us-east1
MACHINE="e2-micro"                  # shape ฟรีถาวร
NAME="crypto-bot"
IMAGE_FAMILY="ubuntu-2404-lts-amd64"
IMAGE_PROJECT="ubuntu-os-cloud"
DISK_GB="30"                        # Always Free: standard persistent disk สูงสุด 30GB
SSH_USER="ubuntu"
SSH_KEY="$HOME/.ssh/id_rsa.pub"
STARTUP="$(cd "$(dirname "$0")" && pwd)/deploy-cloudinit.sh"
# ===============================================

[ -f "$SSH_KEY" ] || { echo "ไม่พบ ssh public key: $SSH_KEY (สร้างด้วย: ssh-keygen -t rsa -b 4096)"; exit 1; }
[ -f "$STARTUP" ] || { echo "ไม่พบ $STARTUP (วางไว้โฟลเดอร์เดียวกัน)"; exit 1; }
command -v gcloud >/dev/null 2>&1 || { echo "ไม่พบ gcloud CLI — ติดตั้งก่อน: https://cloud.google.com/sdk/docs/install"; exit 1; }

PROJ_ARG=()
[ -n "$PROJECT" ] && PROJ_ARG=(--project "$PROJECT")

echo ">> เปิด Compute Engine API (ครั้งเดียว, ข้ามถ้าเปิดแล้ว)..."
gcloud services enable compute.googleapis.com "${PROJ_ARG[@]}" 2>/dev/null || true

echo ">> สร้าง VM '$NAME' ($MACHINE) ที่ $ZONE ..."
gcloud compute instances create "$NAME" \
  "${PROJ_ARG[@]}" \
  --zone "$ZONE" \
  --machine-type "$MACHINE" \
  --image-family "$IMAGE_FAMILY" \
  --image-project "$IMAGE_PROJECT" \
  --boot-disk-size "${DISK_GB}GB" \
  --boot-disk-type pd-standard \
  --metadata-from-file startup-script="$STARTUP" \
  --metadata ssh-keys="${SSH_USER}:$(cat "$SSH_KEY")"

echo ">> เปิด firewall ให้ SSH เข้าได้ (ครั้งเดียว, ไม่เป็นไรถ้ามีแล้ว)..."
gcloud compute firewall-rules create allow-ssh-crypto-bot \
  "${PROJ_ARG[@]}" --allow tcp:22 --source-ranges 0.0.0.0/0 2>/dev/null || true

IP=$(gcloud compute instances describe "$NAME" "${PROJ_ARG[@]}" --zone "$ZONE" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

echo ""
echo "✅ สร้าง VM สำเร็จ! startup-script กำลังดีพลอยบอทให้เองในพื้นหลัง (รอ ~2-3 นาที)"
echo "   PUBLIC IP = $IP"
echo ""
echo "ขั้นต่อไป (รอ ~3 นาทีให้ดีพลอยเสร็จก่อน แล้ว):"
echo "   ssh -i ~/.ssh/id_rsa ${SSH_USER}@${IP}"
echo "   cat ~/NEXT_STEPS.txt"

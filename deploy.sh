#!/bin/bash
# Ghost Schema — One-Click AWS Deploy
# Provisions: EC2 (t3.medium) + EBS (20GB) + Security Group
# Requirements: aws CLI configured, ANTHROPIC_API_KEY and GEMINI_API_KEY set in env

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.medium}"
AMI_ID="${AMI_ID:-}"  # auto-detected if empty
EBS_SIZE="${EBS_SIZE:-20}"
KEY_NAME="${KEY_NAME:-}"  # your EC2 key pair name (required for SSH)
TAG_NAME="ghost-schema"

# ── Validate ─────────────────────────────────────────────────────────────────
[ -z "${ANTHROPIC_API_KEY:-}" ] && { echo "Error: ANTHROPIC_API_KEY not set"; exit 1; }
[ -z "${GEMINI_API_KEY:-}" ]    && { echo "Error: GEMINI_API_KEY not set"; exit 1; }

# ── Fetch latest Amazon Linux 2023 AMI ───────────────────────────────────────
if [ -z "$AMI_ID" ]; then
  echo "→ Fetching latest Amazon Linux 2023 AMI..."
  AMI_ID=$(aws ec2 describe-images \
    --region "$REGION" \
    --owners amazon \
    --filters "Name=name,Values=al2023-ami-*-x86_64" "Name=state,Values=available" \
    --query "sort_by(Images, &CreationDate)[-1].ImageId" \
    --output text)
  echo "  AMI: $AMI_ID"
fi

# ── Security group ────────────────────────────────────────────────────────────
echo "→ Creating security group..."
SG_ID=$(aws ec2 create-security-group \
  --region "$REGION" \
  --group-name "${TAG_NAME}-sg" \
  --description "Ghost Schema" \
  --query "GroupId" --output text 2>/dev/null || \
  aws ec2 describe-security-groups \
    --region "$REGION" \
    --filters "Name=group-name,Values=${TAG_NAME}-sg" \
    --query "SecurityGroups[0].GroupId" --output text)

aws ec2 authorize-security-group-ingress --region "$REGION" \
  --group-id "$SG_ID" --protocol tcp --port 22   --cidr 0.0.0.0/0 2>/dev/null || true
aws ec2 authorize-security-group-ingress --region "$REGION" \
  --group-id "$SG_ID" --protocol tcp --port 8000 --cidr 0.0.0.0/0 2>/dev/null || true

# ── User-data: install Docker + run Ghost Schema ─────────────────────────────
USER_DATA=$(cat <<SCRIPT
#!/bin/bash
set -euo pipefail
# Log to file AND console (visible in EC2 console output)
exec > >(tee /var/log/ghost-schema-init.log) 2>&1
echo "=== Ghost Schema init starting ==="

# AL2023 runs dnf-automatic at boot; wait for the lock to clear
echo "Waiting for dnf lock..."
while fuser /var/lib/rpm/.rpm.lock /var/lib/dnf/metadata_lock.pid >/dev/null 2>&1; do
  sleep 3
done

echo "Installing docker and git..."
dnf install -y docker git

systemctl enable docker
systemctl start docker

# Docker Compose V2 plugin
echo "Installing docker compose..."
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
     -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Mount EBS data volume at /data
DEVICE=\$(lsblk -dno NAME,TYPE | awk '\$2=="disk" && \$1!="nvme0n1" {print \$1; exit}')
if [ -n "\$DEVICE" ] && [ -b "/dev/\$DEVICE" ]; then
  echo "Mounting EBS device /dev/\$DEVICE at /data..."
  mkfs.ext4 -F "/dev/\$DEVICE" 2>/dev/null || true
  mkdir -p /data
  mount "/dev/\$DEVICE" /data
  echo "/dev/\$DEVICE /data ext4 defaults 0 2" >> /etc/fstab
fi

# Clone repo
echo "Cloning repo..."
GIT_TERMINAL_PROMPT=0 git clone --depth 1 \
  https://github.com/MayankChaturvedi/ghost_schema /opt/ghost-schema

cd /opt/ghost-schema

cat > .env <<ENV
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
GEMINI_API_KEY=${GEMINI_API_KEY}
ENV

# Point data volume at EBS mount
sed -i 's|./data:/app/data|/data:/app/data|' docker-compose.yml

echo "Starting docker compose..."
docker compose up -d --build
echo "=== Ghost Schema init complete ==="
SCRIPT
)

# ── Launch instance ───────────────────────────────────────────────────────────
echo "→ Launching EC2 instance..."
KEY_SPEC=""
[ -n "$KEY_NAME" ] && KEY_SPEC="--key-name $KEY_NAME"

INSTANCE_ID=$(aws ec2 run-instances \
  --region "$REGION" \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --security-group-ids "$SG_ID" \
  --user-data "$USER_DATA" \
  $KEY_SPEC \
  --block-device-mappings "[{
    \"DeviceName\":\"/dev/xvdf\",
    \"Ebs\":{\"VolumeSize\":$EBS_SIZE,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":false}
  }]" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$TAG_NAME}]" \
  --query "Instances[0].InstanceId" \
  --output text)

echo "→ Waiting for instance to be running..."
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

PUBLIC_IP=$(aws ec2 describe-instances \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)

echo ""
echo "✓ Ghost Schema deployed!"
echo "  Instance : $INSTANCE_ID"
echo "  Public IP: $PUBLIC_IP"
echo "  URL      : http://$PUBLIC_IP:8000"
echo "  (App starts in ~2 min while Docker builds. EBS volume persists across restarts.)"
echo ""
echo "  To SSH (if key provided): ssh ec2-user@$PUBLIC_IP"
echo "  To tear down: aws ec2 terminate-instances --region $REGION --instance-ids $INSTANCE_ID"

#!/usr/bin/env bash
# Build the Linux cores (x64 and arm64) on one EC2 spot instance, then publish them.
#
#     browser/cloud/build-release.sh core-155.0.8059.12-linux.2
#
# The Chromium checkout lives on a 300 GB gp3 volume in eu-north-1 that is kept
# between releases (fetching Chromium again takes hours), mounted at /work with
# this folder's patches at /work/fury/core. One c7i.24xlarge-class spot instance
# builds both targets in about 3 hours for about $2-3; the instance is terminated
# at the end, the volume stays. eu-north-1 because us-east-2 had no large spot
# capacity on 2026-10-08 and reclaimed two instances mid-build.
#
# Written from the commands that built core-155.0.8059.12-linux.1 on 2026-10-12;
# not yet run as this one script.
set -euo pipefail
tag="${1:?usage: build-release.sh <tag, e.g. core-155.0.8059.12-linux.2>}"
export AWS_PROFILE="${AWS_PROFILE:-jclegg}"
REGION=eu-north-1
VOLUME=vol-0dd7d1c874f76c3ea
AMI=ami-09d835910a77f135c
KEY=fury-build
SECURITY_GROUP=sg-019de6406c201544d
SUBNET=subnet-03e065bcdea0d059e
TYPES=(c7i.24xlarge m7i.24xlarge c6i.32xlarge m6i.32xlarge c6i.24xlarge m6i.24xlarge r6i.24xlarge c7i.16xlarge)
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ssh_to=(ssh -i "$HOME/.ssh/fury-build" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

# SSH in from this machine only.
my_ip="$(curl -fsS https://checkip.amazonaws.com)"
aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SECURITY_GROUP" \
  --protocol tcp --port 22 --cidr "$my_ip/32" 2>/dev/null || true

instance=""
for type in "${TYPES[@]}"; do
  if instance="$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "$type" \
      --key-name "$KEY" --security-group-ids "$SECURITY_GROUP" --subnet-id "$SUBNET" \
      --instance-market-options 'MarketType=spot,SpotOptions={SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}' \
      --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":60,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
      --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=stealthbox-core-build}]' \
      --query 'Instances[0].InstanceId' --output text 2>/dev/null)"; then
    echo "== $type $instance"
    break
  fi
  echo "== no spot capacity for $type"
done
[ -n "$instance" ] || { echo "no spot capacity for any type" >&2; exit 1; }
trap 'aws ec2 terminate-instances --region "$REGION" --instance-ids "$instance" >/dev/null; echo "== terminated $instance"' EXIT

aws ec2 wait instance-running --region "$REGION" --instance-ids "$instance"
aws ec2 attach-volume --region "$REGION" --volume-id "$VOLUME" --instance-id "$instance" --device /dev/sdf >/dev/null
ip="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$instance" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
until "${ssh_to[@]}" "ubuntu@$ip" true 2>/dev/null; do sleep 5; done

"${ssh_to[@]}" "ubuntu@$ip" 'until lsblk -dnpo NAME,SIZE | grep -q " 300G"; do sleep 2; done
  device=$(lsblk -dnpo NAME,SIZE | awk "\$2==\"300G\"{print \$1}")
  sudo mkdir -p /work && sudo mount "$device" /work && sudo chown -R ubuntu /work/fury'
rsync -a --delete -e "${ssh_to[*]}" --exclude src --exclude dist --exclude dist-core --exclude cloud \
  "$here/" "ubuntu@$ip:/work/fury/core/"
rsync -a -e "${ssh_to[*]}" "$here/cloud/build-all.sh" "ubuntu@$ip:/work/build-all.sh"
# The volume's tree already carries the series as of the last release. A new or
# changed patch goes in on the instance first (build/refresh.sh for one patch,
# build/rebase.sh for a new Chromium version) before this builds.
"${ssh_to[@]}" "ubuntu@$ip" 'nohup bash /work/build-all.sh >/dev/null 2>&1 &'

echo "== building (about 3 hours); log: ssh ubuntu@$ip tail -f /work/build-all.log"
until "${ssh_to[@]}" "ubuntu@$ip" 'grep -q "^EXIT=" /work/build-all.log 2>/dev/null'; do sleep 120; done
"${ssh_to[@]}" "ubuntu@$ip" 'tail -3 /work/build-all.log'
"${ssh_to[@]}" "ubuntu@$ip" 'grep -q "^EXIT=0" /work/build-all.log' || { echo "build failed" >&2; exit 1; }

mkdir -p "$here/dist"
rsync -a -e "${ssh_to[*]}" "ubuntu@$ip:/work/fury/core/dist/fury-core-linux-*.tar.xz" "$here/dist/"
"${ssh_to[@]}" "ubuntu@$ip" 'sudo umount /work'
aws ec2 detach-volume --region "$REGION" --volume-id "$VOLUME" >/dev/null

gh release create "$tag" --repo jonclegg/stealthbox --title "Browser core $tag" \
  --notes "Fury core for Linux (x64, arm64) from browser/ at $(git -C "$here" rev-parse --short HEAD)." \
  "$here/dist/fury-core-linux-x64.tar.xz" "$here/dist/fury-core-linux-arm64.tar.xz"
echo "== published $tag; set FURY_CORE_TAG and these checksums in image/Dockerfile:"
shasum -a 256 "$here/dist/fury-core-linux-x64.tar.xz" "$here/dist/fury-core-linux-arm64.tar.xz"

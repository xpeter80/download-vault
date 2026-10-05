#!/bin/sh
set -eu
docker run -d --name nova-download-vault --restart unless-stopped \
  --network nova-invest-edge --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 128 --memory 256m \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
  --log-opt max-size=5m --log-opt max-file=3 \
  -p 127.0.0.1:8773:8000 \
  -v /var/lib/nova-download-vault/config.json:/private/config.json:ro \
  -v /var/lib/nova-download-vault/admin-control:/admin-control \
  -v /var/lib/nova-download-vault/state:/state \
  -v /home/nas3/download/nova-vault:/downloads \
  nova-download-vault:1.8.1

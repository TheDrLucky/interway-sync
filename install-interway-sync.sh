#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

CTID=${CTID:-140}
LXC_HOSTNAME=${LXC_HOSTNAME:-interway-sync}
BRIDGE=${BRIDGE:-vmbr0}
IP_ADDRESS=${IP_ADDRESS:-192.168.10.${CTID}/24}
GATEWAY=${GATEWAY:-192.168.10.1}
ROOTFS_STORAGE=${ROOTFS_STORAGE:-}
TEMPLATE_STORAGE=${TEMPLATE_STORAGE:-}
PRIVILEGED=${INTERWAY_PRIVILEGED:-0}
GOOGLE_KEY=${GOOGLE_KEY:-/root/interway-google.json}
GOOGLE_CALENDAR_ID=${GOOGLE_CALENDAR_ID:-350914e26857c495db40f6b0fab2fa03cb40e80f0296a7e1d55638d295db120d@group.calendar.google.com}
RAW_URL="https://raw.githubusercontent.com/TheDrLucky/interway-sync/main"
INSTALL_DIR=$(mktemp -d /tmp/interway-sync.XXXXXX)
CREATED=0

die() {
    echo "Erreur : $*" >&2
    exit 1
}

cleanup() {
    status=$?
    rm -rf -- "$INSTALL_DIR"
    if ((status != 0 && CREATED)); then
        echo "Échec de l'installation : suppression du nouveau LXC $CTID."
        pct stop "$CTID" --skiplock 1 >/dev/null 2>&1 || true
        pct destroy "$CTID" --purge 1 >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup EXIT

[[ $EUID -eq 0 ]] || die "lance cette commande dans le Shell Proxmox en root"
[[ "$CTID" =~ ^[1-9][0-9]{2,}$ ]] || die "numéro LXC invalide : $CTID"
[[ "$PRIVILEGED" =~ ^[01]$ ]] || die "INTERWAY_PRIVILEGED doit valoir 0 ou 1"
for command in awk curl dpkg grep ip pct pveam pvesm; do
    command -v "$command" >/dev/null || die "commande Proxmox manquante : $command"
done
[[ $(dpkg --print-architecture) == amd64 ]] || die "cet installateur nécessite un serveur Proxmox amd64"
ip link show "$BRIDGE" >/dev/null 2>&1 || die "réseau Proxmox introuvable : $BRIDGE"
if ((PRIVILEGED)); then
    echo "Mode de compatibilité : création d'un LXC privilégié dédié à Interway Sync."
fi

read -r -p "Identifiant Interway : " INTERWAY_USER
read -r -s -p "Mot de passe Interway : " INTERWAY_PASSWORD
echo
[[ "$INTERWAY_USER" =~ ^[0-9]+$ ]] || die "l'identifiant Interway doit contenir uniquement des chiffres"
[[ -n "$INTERWAY_PASSWORD" ]] || die "le mot de passe Interway est vide"
printf '%s\n%s\n' "$INTERWAY_USER" "$INTERWAY_PASSWORD" >"$INSTALL_DIR/credentials"
unset INTERWAY_PASSWORD

if pct config "$CTID" >/dev/null 2>&1; then
    pct exec "$CTID" -- test -f /opt/interway-sync/interway_sync.py || \
        die "le LXC $CTID existe déjà et n'appartient pas à Interway Sync"
    pct status "$CTID" | grep -q running || pct start "$CTID"
    pct push "$CTID" "$INSTALL_DIR/credentials" /etc/interway-sync/credentials --perms 0640
    if [[ -f "$GOOGLE_KEY" ]]; then
        pct push "$CTID" "$GOOGLE_KEY" /etc/interway-sync/google-service-account.json --perms 0640
    fi
    pct exec "$CTID" -- test -f /etc/interway-sync/google-service-account.json || \
        die "clé Google manquante : $GOOGLE_KEY"
    pct exec "$CTID" -- chown root:interway-sync /etc/interway-sync/credentials /etc/interway-sync/google-service-account.json
    pct exec "$CTID" -- systemctl start interway-sync.service
    trap - EXIT
    rm -rf -- "$INSTALL_DIR"
    echo "Mot de passe mis à jour dans le LXC $CTID."
    exit 0
fi

[[ -f "$GOOGLE_KEY" ]] || die "clé Google manquante : $GOOGLE_KEY"

curl -fsSL "$RAW_URL/interway_sync.py" -o "$INSTALL_DIR/interway_sync.py"
curl -fsSL "$RAW_URL/requirements.txt" -o "$INSTALL_DIR/requirements.txt"

if [[ -z "$ROOTFS_STORAGE" ]]; then
    ROOTFS_STORAGE=$(pvesm status --content rootdir --enabled 1 2>/dev/null | awk 'NR > 1 && $3 == "active" {print $1; exit}')
fi
if [[ -z "$TEMPLATE_STORAGE" ]]; then
    TEMPLATE_STORAGE=$(pvesm status --content vztmpl --enabled 1 2>/dev/null | awk 'NR > 1 && $3 == "active" {print $1; exit}')
fi
[[ -n "$ROOTFS_STORAGE" ]] || die "aucun stockage LXC actif trouvé"
[[ -n "$TEMPLATE_STORAGE" ]] || die "aucun stockage de modèles actif trouvé"

pveam update
TEMPLATE=$(pveam available --section system | awk '$2 ~ /^debian-12-standard_.*_amd64.tar.zst$/ {value=$2} END {print value}')
if [[ -z "$TEMPLATE" ]]; then
    TEMPLATE=$(pveam available --section system | awk '$2 ~ /^debian-13-standard_.*_amd64.tar.zst$/ {value=$2} END {print value}')
fi
[[ -n "$TEMPLATE" ]] || die "modèle Debian 12 ou 13 amd64 introuvable"
if ! pveam list "$TEMPLATE_STORAGE" | awk 'NR > 1 {print $1}' | grep -qx "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}"; then
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
    --hostname "$LXC_HOSTNAME" \
    --cores 1 \
    --memory 1024 \
    --swap 512 \
    --rootfs "${ROOTFS_STORAGE}:8" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=${IP_ADDRESS},gw=${GATEWAY},type=veth" \
    --unprivileged "$((1 - PRIVILEGED))" \
    --features nesting=1 \
    --timezone Europe/Paris \
    --onboot 1 \
    --start 1
CREATED=1

for _ in {1..30}; do
    pct exec "$CTID" -- true >/dev/null 2>&1 && break
    sleep 1
done
pct exec "$CTID" -- true >/dev/null 2>&1 || die "le nouveau LXC ne démarre pas"

for _ in {1..30}; do
    pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
    sleep 2
done
pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 || \
    die "le nouveau LXC n'a pas accès au réseau"

pct exec "$CTID" -- bash -lc '
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates python3 python3-venv
id -u interway-sync >/dev/null 2>&1 || useradd --system --home-dir /var/lib/interway-sync --create-home --shell /usr/sbin/nologin interway-sync
install -d -o root -g root -m 0755 /opt/interway-sync
install -d -o interway-sync -g interway-sync -m 0700 /var/lib/interway-sync /var/lib/interway-sync/profile
install -d -o root -g interway-sync -m 0750 /etc/interway-sync
'

cat >"$INSTALL_DIR/interway-sync.service" <<EOF
[Unit]
Description=Extraction du planning Interway
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=interway-sync
Group=interway-sync
WorkingDirectory=/opt/interway-sync
Environment=PYTHONUNBUFFERED=1
Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/interway-sync/browsers
ExecStart=/opt/interway-sync/.venv/bin/python /opt/interway-sync/interway_sync.py --technicien $INTERWAY_USER --credentials /etc/interway-sync/credentials --profile /var/lib/interway-sync/profile --state /var/lib/interway-sync/state.json --output /var/lib/interway-sync/planning.json --previous-weeks 1 --next-weeks 8 --google-credentials /etc/interway-sync/google-service-account.json --google-calendar $GOOGLE_CALENDAR_ID
PrivateTmp=true
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/interway-sync
TimeoutStartSec=15min
EOF

cat >"$INSTALL_DIR/interway-sync.timer" <<'EOF'
[Unit]
Description=Extraction quotidienne du planning Interway

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true
Unit=interway-sync.service

[Install]
WantedBy=timers.target
EOF

pct push "$CTID" "$INSTALL_DIR/interway_sync.py" /opt/interway-sync/interway_sync.py --perms 0755
pct push "$CTID" "$INSTALL_DIR/requirements.txt" /opt/interway-sync/requirements.txt --perms 0644
pct push "$CTID" "$INSTALL_DIR/credentials" /etc/interway-sync/credentials --perms 0640
pct push "$CTID" "$GOOGLE_KEY" /etc/interway-sync/google-service-account.json --perms 0640
pct push "$CTID" "$INSTALL_DIR/interway-sync.service" /etc/systemd/system/interway-sync.service --perms 0644
pct push "$CTID" "$INSTALL_DIR/interway-sync.timer" /etc/systemd/system/interway-sync.timer --perms 0644

pct exec "$CTID" -- chown root:interway-sync /etc/interway-sync/credentials /etc/interway-sync/google-service-account.json
pct exec "$CTID" -- bash -lc '
set -Eeuo pipefail
python3 -m venv /opt/interway-sync/.venv
/opt/interway-sync/.venv/bin/pip install --disable-pip-version-check -r /opt/interway-sync/requirements.txt
PLAYWRIGHT_BROWSERS_PATH=/opt/interway-sync/browsers /opt/interway-sync/.venv/bin/python -m playwright install --with-deps --only-shell chromium
chmod -R a+rX /opt/interway-sync/browsers
systemctl daemon-reload
systemctl start interway-sync.service
python3 -c '\''import json; data=json.load(open("/var/lib/interway-sync/planning.json")); assert len(data["jours"]) >= 35'\''
systemctl enable --now interway-sync.timer
systemctl is-active --quiet interway-sync.timer
'

CREATED=0
trap - EXIT
rm -rf -- "$INSTALL_DIR"
echo "Terminé : LXC $CTID installé et planning actualisé chaque jour à 06:00."

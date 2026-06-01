#!/usr/bin/env bash
#
# garage-ap.sh — turn the Pi into a standalone Wi-Fi access point for Pilot View.
#
# WHY THIS IS CAREFUL: the Pi 5 has ONE Wi-Fi radio. The moment wlan0 becomes an
# access point it LEAVES the house Wi-Fi — so SSH over the house network drops and
# the Pi has no internet. To avoid locking you out, `on` schedules an automatic
# revert to house Wi-Fi after a few minutes unless you `confirm`.
#
# Typical flow (run the first three while still on house Wi-Fi):
#   sudo bash garage-ap.sh install GB PilotView 'your-strong-wifi-password'
#   sudo bash garage-ap.sh on            # flips to AP; auto-reverts in 10 min
#   # join 'PilotView' on your phone, open  http://10.42.0.1:8000
#   sudo bash garage-ap.sh confirm       # makes the AP permanent
#
# Other commands:
#   sudo bash garage-ap.sh off           # go back to house Wi-Fi (e.g. to update)
#   sudo bash garage-ap.sh status
#
set -uo pipefail

AP_CON="PilotView"          # NetworkManager connection name for the AP
IFACE="wlan0"
REVERT_UNIT="ap-revert"
REVERT_SECS=600             # auto-revert window (seconds)
GATEWAY_IP="10.42.0.1"      # NM 'shared' mode default gateway/host IP
SERVICE="pilot-view"

NMCLI="$(command -v nmcli || echo /usr/bin/nmcli)"

need_root() { [ "$(id -u)" -eq 0 ] || { echo "ERROR: run with sudo"; exit 1; }; }

# Name of an existing wifi connection that ISN'T our AP (the house network).
house_con_active() {
  "$NMCLI" -t -f NAME,TYPE,DEVICE con show --active 2>/dev/null \
    | awk -F: -v ap="$AP_CON" '$2 ~ /wireless/ && $1 != ap {print $1; exit}'
}
house_con_any() {
  "$NMCLI" -t -f NAME,TYPE con show 2>/dev/null \
    | awk -F: -v ap="$AP_CON" '$2 ~ /wireless/ && $1 != ap {print $1; exit}'
}

cmd_install() {
  need_root
  local country="${1:-}" ssid="${2:-PilotView}" psk="${3:-}"
  if [ -z "$country" ] || [ -z "$psk" ]; then
    echo "usage: sudo bash $0 install <COUNTRY_CODE> <SSID> <WIFI_PASSWORD>"
    echo "  e.g. sudo bash $0 install GB PilotView '<choose-a-strong-password>'"
    exit 1
  fi
  if [ "${#psk}" -lt 8 ]; then echo "ERROR: Wi-Fi password must be >= 8 characters"; exit 1; fi

  echo "[1/5] dependencies (needs internet — run while on house Wi-Fi)…"
  command -v iw >/dev/null 2>&1 || apt-get install -y iw >/dev/null 2>&1 || \
    echo "  (warning: could not install 'iw'; AP will still try default regdomain)"
  # NM 'shared' mode needs dnsmasq-base for the built-in DHCP/DNS server.
  dpkg -s dnsmasq-base >/dev/null 2>&1 || apt-get install -y dnsmasq-base >/dev/null 2>&1 || \
    echo "  (warning: could not install dnsmasq-base; shared mode may fail)"

  echo "[2/5] regulatory domain -> $country (persisted across reboots)…"
  if command -v iw >/dev/null 2>&1; then iw reg set "$country" 2>/dev/null || true; fi
  cat >/etc/systemd/system/wifi-regdom.service <<EOF
[Unit]
Description=Set Wi-Fi regulatory domain for Pilot View AP
After=network-pre.target
[Service]
Type=oneshot
ExecStart=/usr/sbin/iw reg set $country
[Install]
WantedBy=multi-user.target
EOF
  systemctl enable wifi-regdom.service >/dev/null 2>&1 || true

  echo "[3/5] creating AP connection '$AP_CON' (SSID '$ssid', staged, NOT active)…"
  "$NMCLI" con delete "$AP_CON" >/dev/null 2>&1 || true
  "$NMCLI" con add type wifi ifname "$IFACE" con-name "$AP_CON" autoconnect no ssid "$ssid" >/dev/null
  "$NMCLI" con modify "$AP_CON" \
    802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
    ipv4.method shared \
    wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
    wifi-sec.psk "$psk" \
    connection.autoconnect no

  echo "[4/5] relaxing the dashboard login + gating the admin terminal…"
  # On the AP the Wi-Fi/WPA2 password gates the *dashboard* (PILOT_VIEW_AUTH=disabled
  # so the phone needs no passkey). The web terminal is a ROOT-CAPABLE shell, so it
  # is gated separately by a random token even when the dashboard login is off —
  # the Wi-Fi password alone must never hand a stranger a shell.
  local term_token; term_token="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 28)"
  mkdir -p "/etc/systemd/system/${SERVICE}.service.d"
  cat >"/etc/systemd/system/${SERVICE}.service.d/override.conf" <<EOF
[Service]
Environment=PILOT_VIEW_AUTH=disabled
Environment=PILOT_VIEW_TERMINAL=enabled
Environment=PILOT_VIEW_TERMINAL_TOKEN=$term_token
EOF
  chmod 600 "/etc/systemd/system/${SERVICE}.service.d/override.conf"
  systemctl daemon-reload
  systemctl restart "$SERVICE" 2>/dev/null || true
  echo
  echo "  >> TERMINAL TOKEN (save this — it's the gate to the admin shell):"
  echo "        $term_token"
  echo "     Open the terminal at:  http://${GATEWAY_IP}:8000/terminal?token=$term_token"
  echo "     (It is remembered in the browser after the first visit.)"

  echo "[5/5] turning off Tailscale…"
  tailscale down >/dev/null 2>&1 || true
  systemctl disable --now tailscaled >/dev/null 2>&1 || true

  echo
  echo "DONE. The AP is staged but NOT active yet (you're still on house Wi-Fi)."
  echo "Next, with your phone ready:  sudo bash $0 on"
}

cmd_on() {
  need_root
  # Capture the house connection BEFORE we flip away from it.
  local house; house="$(house_con_active)"; [ -z "$house" ] && house="$(house_con_any)"

  if [ -n "$house" ]; then
    systemctl stop "${REVERT_UNIT}.timer" >/dev/null 2>&1 || true
    systemd-run --on-active="$REVERT_SECS" --unit="$REVERT_UNIT" \
      "$NMCLI" con up "$house" >/dev/null 2>&1 || true
    echo "Safety net: will auto-revert to '$house' in $((REVERT_SECS/60)) min unless you confirm."
  else
    echo "WARNING: could not identify the house Wi-Fi connection — NO auto-revert scheduled."
    echo "         If the AP fails you'll need a keyboard/monitor on the Pi to recover."
  fi

  echo "Bringing up AP '$AP_CON' (your SSH/house Wi-Fi will drop now)…"
  # Detached so it completes even when this session's link drops.
  systemd-run --unit=ap-now --collect "$NMCLI" con up "$AP_CON" >/dev/null 2>&1 || true

  echo
  echo "  1. Join '$AP_CON' on your phone (one-time Wi-Fi password entry)."
  echo "  2. Open  http://${GATEWAY_IP}:8000"
  echo "  3. If it works:   sudo bash $0 confirm"
  echo "     If it doesn't:  wait $((REVERT_SECS/60)) min (auto) or run  sudo bash $0 off"
}

cmd_finalize() {
  # One-shot: install iw, set the regulatory domain (so AP channels are allowed),
  # set the Wi-Fi password, then make the AP permanent and bring it up.
  # Safe to run from the web terminal — it never restarts the pilot-view service.
  need_root
  local country="${1:-}" psk="${2:-}"
  if [ -z "$country" ] || [ -z "$psk" ]; then
    echo "usage: sudo bash $0 finalize <COUNTRY_CODE> <WIFI_PASSWORD>"
    echo "  e.g. sudo bash $0 finalize GB '<choose-a-strong-password>'"
    exit 1
  fi
  if [ "${#psk}" -lt 8 ]; then echo "ERROR: Wi-Fi password must be >= 8 characters"; exit 1; fi

  echo "[1/3] regulatory domain -> $country (installs iw if needed; needs internet)…"
  command -v iw >/dev/null 2>&1 || apt-get install -y iw >/dev/null 2>&1 || \
    echo "  (warning: could not install 'iw' — AP may be limited without a regdomain)"
  if command -v iw >/dev/null 2>&1; then iw reg set "$country" 2>/dev/null || true; fi
  cat >/etc/systemd/system/wifi-regdom.service <<EOF
[Unit]
Description=Set Wi-Fi regulatory domain for Pilot View AP
After=network-pre.target
[Service]
Type=oneshot
ExecStart=/usr/sbin/iw reg set $country
[Install]
WantedBy=multi-user.target
EOF
  systemctl enable wifi-regdom.service >/dev/null 2>&1 || true

  echo "[2/3] setting the Wi-Fi password on '$AP_CON'…"
  "$NMCLI" con modify "$AP_CON" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk"

  echo "[3/3] making the AP permanent and activating it…"
  cmd_confirm
}

cmd_confirm() {
  need_root
  systemctl stop "${REVERT_UNIT}.timer" >/dev/null 2>&1 || true
  # IMPORTANT: keep house Wi-Fi as an autoconnect FALLBACK (lower priority). If the
  # AP can't start, NM falls back to the house network and the Pi stays reachable —
  # no lockout. The AP wins whenever it's available (higher priority).
  "$NMCLI" con modify "$AP_CON" connection.autoconnect yes connection.autoconnect-priority 100 2>/dev/null || true
  echo "AP set to autoconnect (priority). House Wi-Fi kept as a fallback."
  echo "Activating the AP now — this connection will drop; rejoin '$AP_CON' on your phone."
  # Detached so it completes even though bringing up the AP kills this session.
  systemd-run --unit=ap-up --collect "$NMCLI" con up "$AP_CON" >/dev/null 2>&1 \
    || "$NMCLI" con up "$AP_CON" >/dev/null 2>&1 || true
  echo "Open  http://${GATEWAY_IP}:8000   (terminal at /terminal). To undo: sudo bash $0 off"
}

cmd_off() {
  need_root
  systemctl stop "${REVERT_UNIT}.timer" >/dev/null 2>&1 || true
  "$NMCLI" con modify "$AP_CON" connection.autoconnect no 2>/dev/null || true
  local house; house="$(house_con_any)"
  if [ -n "$house" ]; then
    "$NMCLI" con modify "$house" connection.autoconnect yes 2>/dev/null || true
    "$NMCLI" con up "$house" 2>/dev/null || true
    echo "Reverted to house Wi-Fi ('$house')."
  else
    echo "Reverted AP autoconnect off, but couldn't find a house Wi-Fi connection to bring up."
  fi
}

cmd_status() {
  echo "== devices =="; "$NMCLI" dev status | grep -E "DEVICE|wlan0|tailscale" || true
  echo "== wifi connections =="
  "$NMCLI" -t -f NAME,TYPE,AUTOCONNECT con show | grep -iE "wireless|$AP_CON" || true
  echo "== revert timer =="; systemctl is-active "${REVERT_UNIT}.timer" 2>/dev/null || echo "inactive"
}

case "${1:-}" in
  install)  shift; cmd_install "$@";;
  finalize) shift; cmd_finalize "$@";;
  on)       cmd_on;;
  confirm)  cmd_confirm;;
  off)      cmd_off;;
  status)   cmd_status;;
  *) echo "usage: sudo bash $0 {install <CC> <SSID> <PSK> | finalize <CC> <PSK> | on | confirm | off | status}";;
esac

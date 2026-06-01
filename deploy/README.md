# Deploying Pilot View on the Raspberry Pi

The app runs as a non-root systemd service (`pilot-view`) under the `pilot-view`
user, repo at `/home/pilot-view/pilot-view`, Python venv at `.venv`.

## First install

```bash
# 1. systemd unit + drop-ins
sudo cp deploy/pilot-view.service /etc/systemd/system/pilot-view.service
sudo mkdir -p /etc/systemd/system/pilot-view.service.d
sudo cp deploy/env.conf /etc/systemd/system/pilot-view.service.d/env.conf
sudo systemctl daemon-reload
sudo systemctl enable --now pilot-view

# 2. verify
systemctl status pilot-view      # should be: active (running)
curl -s localhost:8000/api/auth/status
```

`Restart=always` brings the app back after a crash or power-cut; `ExecStartPre`
compile-checks the core modules so a syntax-broken `git pull` fails fast and
loudly instead of crash-looping silently.

## Updating

```bash
cd ~/pilot-view && git pull --ff-only && sudo systemctl restart pilot-view
```

(Also available as the "Update app" button in the web terminal.)

## Environment flags

Set these in `.env` (or a systemd drop-in). The app loads `.env` at startup.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PILOT_VIEW_SOURCE` | `orbbec` | Camera source. Unknown values fall back to `orbbec` with a log line. |
| `PILOT_VIEW_AUTH` | `enabled` | `disabled` relaxes the **dashboard** login (used on the AP, where Wi-Fi/WPA2 is the gate). Does NOT affect the terminal. |
| `PILOT_VIEW_TERMINAL` | `disabled` | The web terminal is **opt-in**. Set `enabled` to turn it on. |
| `PILOT_VIEW_TERMINAL_TOKEN` | _(unset)_ | Token required to open the terminal (`/terminal?token=…`). If the terminal is enabled with no token set, a random one is minted and printed to the journal. |

### The web terminal is a root-capable shell

It is therefore gated independently of the dashboard:

* off unless `PILOT_VIEW_TERMINAL=enabled`;
* a valid login session always authorises;
* otherwise a `?token=` matching `PILOT_VIEW_TERMINAL_TOKEN` is required;
* `PILOT_VIEW_AUTH=disabled` (dashboard relax) never, on its own, grants a shell.

`scripts/garage-ap.sh install` enables the terminal with a freshly generated
token and prints it — save that token; it's the gate to the admin shell on the AP.

### Optional: scope the terminal's sudo

The service user already runs non-root. To further limit what the terminal can
do, install the scoped sudoers allowlist (validate with `visudo -c` first):

```bash
sudo cp deploy/pilot-view.sudoers.example /etc/sudoers.d/pilot-view
sudo chmod 440 /etc/sudoers.d/pilot-view
sudo visudo -c
```

## Keeping the disk healthy

The OrbbecSDK file log is turned down to ERROR at startup (it otherwise grows
hundreds of MB/day). To also cap the journal:

```bash
sudo sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
```

`/api/diagnostics` reports `disk.low = true` once the root filesystem passes 90%.

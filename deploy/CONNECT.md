# Connecting to the Pi and deploying — runbook for another machine

Precise, reproducible steps to reach the Pilot View Raspberry Pi from a *different*
PC and push a new project onto it. Written to be followed literally.

## The facts (verified on the live Pi)

| Thing | Value |
| --- | --- |
| Pi hostname (mDNS) | `pilot-view.local` |
| Pi IP (house WiFi, DHCP — may change) | `192.168.1.164` |
| Login user | `pilot-view` |
| Repo path on the Pi | `/home/pilot-view/pilot-view` |
| Git remote (Pi + PC) | `git@github.com:cgarryZA/pilot-view.git` |
| OS / arch | Debian 13 (trixie), `aarch64` |
| Python venv on Pi | `/home/pilot-view/pilot-view/.venv/bin/python` (3.13.5) |
| systemd service | `pilot-view` (enabled → auto-starts on boot) |

The Pi lives on the **house WiFi**. It can temporarily become its own WiFi access
point (`scripts/garage-ap.sh`), but that mode auto-reverts and was never made
permanent, so after a normal power-on it rejoins the house network.

---

## Step 0 — prerequisites on the new PC

- **Use a native OpenSSH client.** On Windows use **PowerShell** (`ssh.exe`), *not*
  Git Bash — Git Bash's ssh mishandles the key passphrase; the Windows ssh-agent
  holds the key. On macOS/Linux any terminal is fine.
- Have **git** installed.
- Have an **SSH keypair** (`ssh-keygen -t ed25519` if you don't).

## Step 1 — get THIS PC's SSH key trusted by the Pi  ⚠️ the usual blocker

SSH auth is **per-machine**. The Pi currently authorizes exactly one key (the
original PC's). A new PC's key is **not** trusted yet, so `ssh` will reject you
until you add it. Do this once:

1. Print the new PC's **public** key:
   ```powershell
   Get-Content $HOME\.ssh\id_ed25519.pub    # Windows
   # cat ~/.ssh/id_ed25519.pub              # macOS/Linux
   ```
2. Append that line to the Pi's `~/.ssh/authorized_keys`. Pick whichever you can do:
   - **From a machine already trusted** (e.g. the original PC):
     ```bash
     ssh pilot-view@192.168.1.164 "echo 'ssh-ed25519 AAAA...new-pc-key...' >> ~/.ssh/authorized_keys"
     ```
   - **With a keyboard/monitor on the Pi**, or if password SSH is enabled, log in
     and paste it in yourself.
   - `ssh-copy-id pilot-view@192.168.1.164` also works if password login is on.

   > There is no way around this from an untrusted PC alone — you need one trusted
   > path (another PC, the console, or a password) to introduce the new key.

### Step 1a — HANDSHAKE: how the assistant on the new PC gets authorized

> **Read this if you are a Claude/assistant running on a NEW PC that is not yet
> trusted by the Pi or by GitHub.** You cannot authenticate yourself — the user
> must carry credentials between machines for you. Do NOT ask the user for
> passwords or private keys; collect only public info and let the *other* side
> (the already-trusted machine, e.g. the original PC and its assistant) do the
> granting. Follow this exactly:
>
> 1. **Generate a key on the new PC if none exists** (safe, no secrets exposed):
>    ```powershell
>    if (-not (Test-Path $HOME\.ssh\id_ed25519.pub)) { ssh-keygen -t ed25519 -C "$env:COMPUTERNAME-pilot-view" -f $HOME\.ssh\id_ed25519 -N '""' }
>    ```
>    (macOS/Linux: `test -f ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C "$(hostname)-pilot-view" -f ~/.ssh/id_ed25519 -N ""`)
>
> 2. **Print the PUBLIC key and give it to the user to relay:**
>    ```powershell
>    Get-Content $HOME\.ssh\id_ed25519.pub
>    ```
>    Tell the user, verbatim:
>    *"Please paste this PUBLIC SSH key to the assistant on your original PC (the
>    one already trusted by the Pi) and ask it to authorize this machine. It's a
>    public key — safe to share. I will NOT need your password or any private key."*
>
> 3. **Also hand the user the GitHub-access question**, because pushing needs the
>    new PC trusted by GitHub too (separate from the Pi):
>    - Ask: *"Will I push to GitHub over SSH or HTTPS on this machine?"*
>    - If **SSH**: the same public key from step 2 must be added to the user's
>      GitHub account → **[github.com/settings/keys](https://github.com/settings/keys)** → *New SSH key*. The user does this in their browser; you never see a token.
>    - If **HTTPS**: the user must supply a Personal Access Token via the OS
>      credential manager / `git credential` — you should NOT store or print it.
>      Prefer SSH.
>
> 4. **What to collect and pass back to the trusted side** (so the original PC's
>    assistant can grant access) — give the user this checklist to relay:
>    - the new PC's **public** key line (step 2)
>    - the **hostname/OS** of the new PC (`hostname`, `ver`/`uname -a`) so the
>      grant can be labeled
>    - which repo/branch you intend to push (this repo, branch `main`)
>
> 5. **Wait for confirmation**, then verify BEFORE trying to push:
>    ```powershell
>    ssh -o BatchMode=yes pilot-view@pilot-view.local "echo pi-ok"     # Pi trust
>    ssh -T git@github.com                                             # GitHub trust (prints your username)
>    ```
>    Only once both succeed should you `git push`.

### For the assistant on the ALREADY-TRUSTED PC — how to grant the new machine

> When the user relays a new PC's **public** key, authorize it like this (you have
> a working `ssh pilot-view` and the user's GitHub is set up in the browser):
>
> - **Pi SSH access** — append the pasted public key to the Pi's authorized_keys
>   (append-only; never overwrite):
>   ```powershell
>   # Replace the quoted string with the EXACT public key line the user pasted.
>   ssh pilot-view "grep -qxF 'PASTE_PUBLIC_KEY_HERE' ~/.ssh/authorized_keys || echo 'PASTE_PUBLIC_KEY_HERE' >> ~/.ssh/authorized_keys; echo done"
>   ```
>   Then confirm: `ssh pilot-view "wc -l ~/.ssh/authorized_keys"` (count should go up by 1).
> - **GitHub access** — you cannot add a key to the user's GitHub account yourself;
>   tell the user to paste the same public key at
>   [github.com/settings/keys](https://github.com/settings/keys). That's a browser
>   action only they can do.
> - Report back to the user so they can tell the new PC it's cleared to verify + push.

## Step 2 — reach the Pi (make sure it's powered on and booted ~1 min)

Try the mDNS name first, fall back to the IP:

```powershell
ssh pilot-view@pilot-view.local          # preferred
ssh pilot-view@192.168.1.164             # fallback if .local doesn't resolve
```

- `.local` needs mDNS/Bonjour. Windows 10+/macOS have it; if it fails, use the IP.
- If the DHCP IP has changed, find it from your router, or (on a trusted machine on
  the LAN) `ping pilot-view.local` to see the current address.
- First connection asks to trust the host fingerprint → `yes`.

**Optional quality-of-life:** add a host alias so `ssh pilot-view` just works.
Put this in the new PC's `~/.ssh/config`:
```
Host pilot-view
    HostName pilot-view.local
    User pilot-view
    IdentityFile ~/.ssh/id_ed25519
    ForwardAgent yes
```
`ForwardAgent yes` forwards your GitHub key to the Pi, so `git pull` on the Pi can
authenticate to GitHub as you (the Pi uses the SSH remote `git@github.com:...`).

## Step 3 — sanity-check the connection

```bash
ssh pilot-view "whoami; systemctl is-active pilot-view; \
  cd ~/pilot-view && git rev-parse --abbrev-ref HEAD && git log --oneline -1; \
  curl -s -o /dev/null -w 'app:%{http_code}\n' http://localhost:8000/api/auth/status"
```
Expect: `pilot-view`, `active`, branch `main`, a commit line, `app:200`.

---

## Step 4 — push a new project / changes onto the Pi

The Pi does **not** receive code directly from the PC. Both sides share GitHub as
the hub. Flow:

1. **On the PC** — commit and push to the branch the Pi tracks (`main`):
   ```bash
   git add -A
   git commit -m "your change"
   git push origin main
   ```
2. **On the Pi** — pull and restart the service:
   ```bash
   ssh pilot-view "cd ~/pilot-view && git pull --ff-only && \
     sudo systemctl restart pilot-view && systemctl status pilot-view --no-pager"
   ```
   Expect `active (running)`. The unit compile-checks core modules on start
   (`ExecStartPre`), so a syntax-broken pull fails loudly instead of crash-looping.

### If the new project needs new Python deps
The venv is at `~/pilot-view/.venv`. Install into it before restarting:
```bash
ssh pilot-view "~/pilot-view/.venv/bin/pip install -r ~/pilot-view/requirements.txt"
```

### If it's a genuinely separate project (not this repo)
Clone it beside this one and give it its own systemd unit (mirror
`deploy/pilot-view.service`; use a different port and service name). Keep
`pilot-view` running on port 8000 unless you mean to replace it.

---

## Gotchas (learned the hard way)

- **PowerShell, not Git Bash**, for ssh on Windows.
- **mDNS may not resolve** on some networks/VPNs → use the IP.
- **DHCP IP can change** after a long power-off → don't hardcode it; prefer `.local`.
- **AP mode**: if someone ran `garage-ap.sh on/confirm`, the Pi may be on its own
  WiFi `PilotView` at `10.42.0.1` instead of the house LAN. `garage-ap.sh off`
  reverts it. After a plain reboot it's on the house WiFi.
- **Web terminal** (browser shell, alternative to SSH): it's opt-in and token-gated.
  On the house-WiFi dev config it's off; when enabled it's reachable at
  `http://<pi>:8000/terminal?token=<PILOT_VIEW_TERMINAL_TOKEN>`.
- **The app is not root** and auto-starts via systemd; you rarely need to run it by
  hand. To run manually for debugging:
  `ssh pilot-view "cd ~/pilot-view && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001"`.

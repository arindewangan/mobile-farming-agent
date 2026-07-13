# Deploying the agent on an edge device (Raspberry Pi, or any always-on box)

An "edge device" is any machine capable of running this agent with phones
attached — a Raspberry Pi is the common choice, but a spare mini-PC or old
laptop works exactly the same way. This agent (the same code that runs
locally) can also run in **listen mode** on that separate machine, with your
phones attached over USB. Once connected, its devices behave *identically*
to directly-attached ones in the dashboard: same mirror, same bulk actions,
same recipes. There's no separate, cut-down code path.

## How it works

- **Client mode** (`--backend ws://...`, the default): the agent dials OUT
  to the dashboard's backend. This is what you run on the same machine as
  the dashboard, or anywhere already trusted on your LAN.
- **Listen mode** (`--listen host:port`): the agent instead hosts its own
  token-authenticated WebSocket server; the dashboard's backend dials IN.
  This is what you run on an edge device — the dashboard reaches out to it
  (over Tailscale, LAN, or the internet), rather than the device needing to
  know the dashboard's address.

Either way, the exact same message protocol drives it (register, device
list, commands, mirror frames) — that's the whole point.

## Install (one command)

Copy `mobilefarm-agent.zip` to the target machine (a Raspberry Pi or any
Debian/Ubuntu box with phones attached over USB) any way you like — `scp`,
a USB stick, whatever — then:

```bash
unzip mobilefarm-agent.zip
cd mobilefarm-agent
sudo bash setup.sh my-edge-device     # arg = a name for this agent
```

Or, if you'd rather work from a git checkout instead of the zip:

```bash
cd agent
sudo bash setup.sh my-edge-device
```

**`setup.sh` handles every prerequisite itself** — it checks for and
installs (via `apt-get`) whatever's missing: `python3`, `python3-venv`,
`python3-pip`, `adb` (`android-tools-adb`), `usbutils`, `curl`, `unzip`; it
also installs the Tailscale binary if it isn't present yet. It then copies
the agent to `/opt/mobilefarm-agent`, creates a venv, installs the Python
deps (~40MB, includes OpenCV/RapidOCR for the no-LLM vision helpers — same
as the local agent), and registers + starts a systemd service
(`mobilefarm-agent`) running in `--listen` mode on port 8091, with its local
admin panel on 8090.

The script is **safe to re-run** — running it again (e.g. after copying a
newer `mobilefarm-agent.zip`) stops the service, re-syncs the code, and
restarts. It never touches `/opt/mobilefarm-agent/data/` (connection
tokens, admin password, ban list), so nothing needs re-configuring after an
update.

Plug in your phones (`adb devices` should list them), then open the admin
panel to set up a connection. The setup script prints the exact URLs and
where to find the first-run admin password at the end.

See the in-app **"Set up new edge device"** guide (Edge Devices page in the
dashboard) for the SSH / manual / Tailscale walkthroughs with copy-pasteable
commands — this doc is the reference version of the same steps.

## Tailscale (recommended, for a fixed address)

`setup.sh` already installed the Tailscale binary. Logging in is a one-time
interactive step (it needs to open a browser link), so finish it yourself
right after setup:

```bash
sudo tailscale up
tailscale ip -4     # e.g. 100.101.102.103 — the device's fixed address
```

To skip the interactive step entirely (e.g. scripted/headless provisioning),
generate an auth key in the Tailscale admin console and pass it in up front:

```bash
TS_AUTHKEY=tskey-xxxxx sudo -E bash setup.sh my-edge-device
```

**The machine running the dashboard's backend needs to be on the same
tailnet too** (install Tailscale there, `tailscale up`). Once both are on
the tailnet, the backend can dial `ws://100.x.x.x:8091` directly and
privately — no port forwarding, no public exposure.

If the backend genuinely can't join the tailnet, you can instead expose
port 8091 with `sudo tailscale funnel 8091` for a public
`wss://<name>.<tailnet>.ts.net` URL — understand that this makes the port
reachable by anyone who knows the URL, so the connection token becomes your
only line of defense; rotate it if you ever suspect it leaked.

## Create a connection + add it in the dashboard

1. Open the admin panel: `http://<tailscale-ip>:8090` (or `http://<lan-ip>:8090`
   if the dashboard is on the same network). The first-run password is
   printed via `journalctl -u mobilefarm-agent -f`, or read
   `/opt/mobilefarm-agent/data/admin_password.txt`.
2. Unlock it, then **create a connection** (give it a name and a scope —
   local, remote, or both). Copy its token.
3. In the dashboard, **Add device** → **Edge device** → paste in:
   - **Name**: whatever you like
   - **Address**: `ws://<tailscale-or-lan-ip>:8091`
   - **Token**: the one you just copied
4. Its devices appear in the Fleet page within a few seconds, tagged with
   this agent's id — group by connection to see them separately, or view
   everything together.

You can create multiple connections (e.g. to re-issue a token without
losing the old one, to organize access, or to let a second dashboard watch
this box) — each is independent and can be individually enabled/disabled/
deleted from the admin panel. Multiple can be connected to this agent at
once: whichever connects first drives the device (taps/swipes/commands);
any later one is accepted read-only — it sees live device/status updates but
can't send commands, so two control planes can never race each other on the
same physical screen.

## Updating

Copy a fresh `mobilefarm-agent.zip` (or `git pull` a checkout) onto the
machine and re-run the same command:

```bash
unzip -o mobilefarm-agent.zip
cd mobilefarm-agent
sudo bash setup.sh my-edge-device
```

`setup.sh` stops the service, re-syncs the code and dependencies, and
restarts it. `/opt/mobilefarm-agent/data/` (connections, admin password,
ban list) is untouched by updates.

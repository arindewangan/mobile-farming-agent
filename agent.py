"""
Node agent — the replicable unit of the fleet.

Runs on any host that has phones attached and `adb` on PATH. It:
  1. Connects up to the control plane over WebSocket.
  2. Enumerates its local devices and registers them.
  3. Executes commands routed to it and returns results.
  4. Streams mirror frames for devices the dashboard is watching.

Scale-out later = run more of these, one per phone-host. The control plane
does not change.

Usage:
    python agent.py --backend ws://CONTROL_PLANE:8000/ws/agent

Per-device resource cost & scaling limits
------------------------------------------
Every attached device that's actively in use costs this host STANDING
resources, tracked in per-serial dicts on the Agent instance and torn down
in `refresh_devices()` when a device disappears:
  - `mirror_tasks`   — one asyncio task per device with a live screen-mirror
    subscriber (H.264 or JPEG loop), the single biggest cost: continuous
    screencap/encode + a websocket frame stream.
  - `scrcpy`         — a scrcpy control server + its persistent socket per
    device once any input/mirror action has touched it (the primary
    low-latency input path).
  - `minitouch`      — only when ENABLE_MINITOUCH=1 (rooted/emulator only);
    another persistent per-device socket.
  - `_input_shells` / `_input_workers` — a persistent adb shell + an ordered
    dispatch task per device once it's received any tap/swipe/key input.

None of this is capped — there's no ceiling and, until now, no visibility
into how loaded a given host actually is. A rough starting budget: idle
(registered but unused) devices are cheap, a handful of dollars of RAM/CPU
each; a device with an ACTIVE mirror subscriber or mid-recipe is the real
cost, and is where a modest host (a Raspberry Pi 4/5-class board) starts to
strain somewhere in the 8-15 concurrently-mirroring-or-busy range — well
below that for simultaneous H.264 mirror streams specifically, since each
is a continuous encode+network cost. Watch the live signal instead of a
fixed number: every agent heartbeat now carries a `load` snapshot (device
count, active mirror/scrcpy/input-worker counts, and — where the OS exposes
it — 1-minute load average and memory-used %), surfaced per-agent via
GET /api/agents and shown in the dashboard. When that starts climbing
under normal usage, it's time to split devices across a second host rather
than adding more to this one.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import json
import os
import socket
import sys
import time

import websockets

import adb
import appsetup
import clashtunnel
import crashmonitor
import detect
import droidrun
import googleaccounts
import playstore
import preflight
import recipeui
import humanize
import onboard
import deviceinput
import minicap
import minitouch as mt
import githubupdate
import ollamaadmin
import scrcpycontrol
import scripting
import selfupdate
import settingsauto
import vision
import youtube
import fingerprint_spoofer

POLL_DEVICES_SEC = 4.0
HEARTBEAT_SEC = 4.0  # liveness ping; must stay well under the backend reaper (20s)
WATCHDOG_SEC = 45.0  # tunnel watchdog: revive fail-closed (core-dead) protected devices
MIRROR_FPS = 6.0  # screencap fallback only; minicap runs at its own ~30-40fps
PROBE_RETRY_COOLDOWN_S = 20.0  # don't hammer adb for a device that just failed to answer getprop


def _read_version() -> str:
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


VERSION = _read_version()

# minitouch needs shell access to the touchscreen /dev/input node, which
# non-rooted consumer devices don't grant (it silently misdetects and crashes).
# Off by default → reliable adb-input gesture path. Set ENABLE_MINITOUCH=1 for
# rooted devices / emulators where minitouch works and is much faster.
ENABLE_MINITOUCH = os.environ.get("ENABLE_MINITOUCH", "0") == "1"


class Agent:
    def __init__(self, backend_url: str, agent_id: str) -> None:
        self.backend_url = backend_url
        self.agent_id = agent_id
        self.ws: websockets.WebSocketClientProtocol | None = None
        # Set whenever self.ws is None (no connection being served), cleared
        # while one is active — lets a superseding connection (see listen()'s
        # handler) wait for the old one's _serve_connection to fully unwind
        # before starting its own, instead of racing to set self.ws first.
        self._ws_closed = asyncio.Event()
        self._ws_closed.set()
        self.devices: dict[str, dict] = {}
        self.mirror_tasks: dict[str, asyncio.Task] = {}
        self.minitouch: dict[str, mt.Minitouch] = {}  # persistent input sessions
        self._minicap_deployed: set[str] = set()  # serials with minicap pushed
        self._touch_start: dict[str, tuple[int, int]] = {}  # adb-path touch
        self._touch_last: dict[str, tuple[int, int]] = {}
        self._touch_t: dict[str, float] = {}  # adb-path touch-down time (hold detect)
        self._tunnel_busy: set[str] = set()  # devices under active tunnel ops (watchdog skips)
        self._minitouch_unavailable: set[str] = set()  # devices w/o shell touch node
        self._input_shells: dict[str, deviceinput.InputShell] = {}  # persistent adb shell
        # scrcpy control server = instant rootless input (primary touch path)
        self.scrcpy: dict[str, scrcpycontrol.ScrcpyControl] = {}
        self._scrcpy_deployed: set[str] = set()
        self._scrcpy_unavailable: set[str] = set()
        self._scrcpy_start_lock = asyncio.Lock()
        # Per-device ordered input pipeline (preserves gesture event order).
        self._input_queues: dict[str, asyncio.Queue] = {}
        self._input_workers: dict[str, asyncio.Task] = {}
        self._mt_start_lock = asyncio.Lock()  # serialize minitouch startup
        self._send_lock = asyncio.Lock()
        self._recorders: dict[str, asyncio.subprocess.Process] = {}  # serial -> screenrecord proc
        self._record_locks: dict[str, asyncio.Lock] = {}  # per-serial mutex: record_start/stop can't race
        self._tunnel_locks: dict[str, asyncio.Lock] = {}  # per-serial mutex: tunnel_* ops can't race
        # --listen (server) mode only — who's currently connected, and how much
        # traffic this session has moved, for the local admin panel's status view.
        self.current_peer: dict | None = None  # {"ip", "scope", "connected_since", "connection_id", "connection_name"}
        self.bytes_sent = 0
        self.bytes_received = 0
        # --listen mode only — additional, read-only connections beyond the
        # primary above (self.ws): a second/third/etc. token can connect at
        # the same time (e.g. a second dashboard just watching this box) but
        # only the primary drives commands/input — see listen()'s docstring
        # for why only one connection can safely touch the physical device.
        # Keyed by connection id (from the token store), so a reconnect under
        # the same token replaces its own entry instead of piling up stale ones.
        self.observers: dict[str, dict] = {}  # connection_id -> {"ws","lock","ip","scope","connection_name","connected_since"}
        # --listen mode only: which serials to leave out of what's advertised
        # to the control plane (still visible/manageable from this admin panel).
        self.hidden_store = None
        # rolling log of recent inbound connection attempts (accepted + rejected),
        # newest first, for the admin panel's Security tab.
        self.connection_log: collections.deque = collections.deque(maxlen=50)

    def _log_connection_attempt(self, ip: str | None, scope: str, outcome: str, reason: str | None = None,
                                 connection_name: str | None = None) -> None:
        self.connection_log.appendleft({
            "at": time.time(), "ip": ip, "scope": scope, "outcome": outcome,
            "reason": reason, "connection_name": connection_name,
        })

    def _visible_devices(self) -> dict:
        """`self.devices` minus anything hidden from the dashboard — this is
        what actually goes out over the wire; `self.devices` itself always
        stays complete for local use (the admin panel's own devices list)."""
        if not self.hidden_store:
            return self.devices
        hidden = self.hidden_store.list_hidden()
        return {s: info for s, info in self.devices.items() if s not in hidden}

    @staticmethod
    def _needs_reprobe(info: dict) -> bool:
        """True if a previous device_info() attempt for this serial never
        actually succeeded (raised, e.g. adb shell getprop timed out) - worth
        retrying rather than caching that failure forever, which otherwise
        leaves a device permanently stuck showing "unknown" in the dashboard
        even after it recovers. Backed off by PROBE_RETRY_COOLDOWN_S so a
        persistently unresponsive device isn't re-probed on every 4s poll."""
        if "error" not in info:
            return False
        return (time.monotonic() - info.get("_probe_failed_at", 0)) >= PROBE_RETRY_COOLDOWN_S

    # -- device enumeration ------------------------------------------------
    async def refresh_devices(self) -> bool:
        """Return True if the set of ready devices changed."""
        serials = await adb.list_serials()
        changed = set(serials) != set(self.devices.keys())
        prev_serials = set(self.devices.keys())
        new: dict[str, dict] = {}
        for s in serials:
            if s in self.devices and not self._needs_reprobe(self.devices[s]):
                new[s] = self.devices[s]
            else:
                try:
                    new[s] = await adb.device_info(s)
                except Exception as e:  # noqa: BLE001
                    new[s] = {"model": "unknown", "android": "?", "state": "device",
                              "error": str(e) or "device didn't respond in time",
                              "_probe_failed_at": time.monotonic()}
        self.devices = new
        # Start the continuous crash/ANR watcher for devices seen for the
        # first time — stopped below, symmetrically, for ones that left.
        for s in set(self.devices) - prev_serials:
            crashmonitor.start(s, self._report_crash_event)
        # Stop mirror tasks + input sessions for devices that disappeared.
        for s in list(self.mirror_tasks):
            if s not in self.devices:
                self._stop_mirror(s)
        for s in list(self.minitouch):
            if s not in self.devices:
                asyncio.create_task(self._drop_minitouch(s))
        for s in list(self._input_workers):
            if s not in self.devices:
                self._stop_input_worker(s)
        for s in list(self._input_shells):
            if s not in self.devices:
                sh = self._input_shells.pop(s)
                asyncio.create_task(sh.stop())
        for s in list(self.scrcpy):
            if s not in self.devices:
                asyncio.create_task(self._drop_scrcpy(s))
        for s in prev_serials - set(self.devices):
            crashmonitor.stop(s)
        self._minicap_deployed &= set(self.devices)  # forget departed devices
        self._scrcpy_deployed &= set(self.devices)
        return changed

    async def _report_crash_event(self, serial: str, event: dict) -> None:
        """crashmonitor's on_event callback — pushes a new crash/ANR straight
        to the control plane so it lands in that device's flag history
        instead of only ever being visible via an on-demand logcat dump."""
        try:
            await self._broadcast({
                "type": "crash_event", "serial": serial,
                "kind": event["kind"], "package": event.get("package", "unknown"),
                "detail": event.get("detail", ""),
            })
        except Exception:  # noqa: BLE001
            pass

    # -- mirror streaming --------------------------------------------------
    def _start_mirror(self, serial: str, mode: str) -> None:
        if serial in self.mirror_tasks or serial not in self.devices:
            return
        loop = self._h264_loop if mode == "h264" else self._jpeg_loop
        self.mirror_tasks[serial] = asyncio.create_task(loop(serial))

    def _stop_mirror(self, serial: str) -> None:
        task = self.mirror_tasks.pop(serial, None)
        if task:
            task.cancel()

    async def _wake(self, serial: str) -> None:
        """Wake the screen so the mirror shows a live image, not a black frame."""
        try:
            await adb.keyevent(serial, "KEYCODE_WAKEUP")
        except Exception:  # noqa: BLE001
            pass

    async def _jpeg_loop(self, serial: str) -> None:
        """JPEG mirror. Prefers minicap (~30-40fps); falls back to screencap."""
        info = self.devices.get(serial, {})
        abi = info.get("abi", "")
        w = int(info.get("width", 1080))
        h = int(info.get("height", 1920))
        await self._wake(serial)
        # Pre-warm input so the first tap is instant: scrcpy control (primary)
        # + the adb-shell channel (fallback for keys/text and non-scrcpy paths).
        asyncio.create_task(self._ensure_scrcpy(serial))
        asyncio.create_task(self._input_shell(serial).start())
        if ENABLE_MINITOUCH:
            asyncio.create_task(self._ensure_minitouch(serial))

        if minicap.binaries(abi)[0]:
            cap = minicap.Minicap(serial, w, h, scale=0.5)
            try:
                if serial not in self._minicap_deployed:
                    await minicap.deploy(serial, abi)
                    self._minicap_deployed.add(serial)
                await cap.start()
                async for jpg in cap.frames():
                    await self._send_frame(serial, jpg)
                return
            except asyncio.CancelledError:
                await cap.stop()
                return
            except Exception as e:  # noqa: BLE001
                print(f"[minicap {serial}] failed ({e}); using screencap", file=sys.stderr)
                await cap.stop()

        # screencap fallback (slow, but universal)
        interval = 1.0 / MIRROR_FPS
        try:
            while True:
                jpeg = await adb.screencap_jpeg(serial)
                if jpeg and self.ws:
                    await self._send_frame(serial, jpeg)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"[mirror {serial}] stopped: {e}", file=sys.stderr)

    async def _h264_loop(self, serial: str) -> None:
        """Primary mirror: stream H.264 from screenrecord, restart on EOF."""
        await self._wake(serial)
        empty_segments = 0
        try:
            while True:
                await self._send({"type": "video_reset", "serial": serial})
                proc = await adb.start_screenrecord(serial)
                bytes_this_segment = 0
                try:
                    while True:
                        chunk = await proc.stdout.read(16384)  # type: ignore[union-attr]
                        if not chunk:
                            break  # time-limit reached or device ended stream
                        bytes_this_segment += len(chunk)
                        b64 = base64.b64encode(chunk).decode("ascii")
                        await self._send({"type": "video", "serial": serial, "data": b64})
                finally:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()

                if bytes_this_segment == 0:
                    empty_segments += 1
                    if empty_segments >= 5:
                        print(
                            f"[mirror {serial}] screenrecord produced no data 5x; "
                            f"this device may not support H.264 screenrecord over stdout.",
                            file=sys.stderr,
                        )
                        return
                    await asyncio.sleep(1.0)
                else:
                    empty_segments = 0
                    await asyncio.sleep(0.2)  # brief settle before next segment
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"[mirror {serial}] stopped: {e}", file=sys.stderr)

    # -- input: minitouch (fast) with adb fallback ------------------------
    async def _ensure_minitouch(self, serial: str) -> mt.Minitouch | None:
        if not ENABLE_MINITOUCH:
            return None  # adb-input path is the reliable default (see top of file)
        m = self.minitouch.get(serial)
        if m is not None:
            return m
        if serial in self._minitouch_unavailable:
            return None  # known: no shell-accessible touch node (needs root)
        info = self.devices.get(serial, {})
        abi = info.get("abi", "")
        if not mt.binary(abi):
            return None
        # Serialize startup so the mirror pre-warm and the first touch can't
        # both start minitouch and fight over the socket.
        async with self._mt_start_lock:
            m = self.minitouch.get(serial)  # re-check after acquiring the lock
            if m is not None:
                return m
            if serial in self._minitouch_unavailable:
                return None
            try:
                await mt.deploy(serial, abi)
                m = mt.Minitouch(
                    serial, int(info.get("width", 1080)), int(info.get("height", 1920))
                )
                await m.start()
                self.minitouch[serial] = m
                print(f"[minitouch {serial}] active (fast input)")
                return m
            except Exception as e:  # noqa: BLE001
                # Most common on non-rooted devices: minitouch can't open the
                # touchscreen /dev/input node. Stop retrying and use adb input.
                if m is not None:
                    try:
                        await m.stop()  # kill the already-spawned on-device server + forward
                    except Exception:  # noqa: BLE001
                        pass
                self._minitouch_unavailable.add(serial)
                print(
                    f"[minitouch {serial}] unavailable ({e}); using adb input path",
                    file=sys.stderr,
                )
                return None

    async def _drop_minitouch(self, serial: str) -> None:
        m = self.minitouch.pop(serial, None)
        if m:
            await m.stop()

    async def _tap(self, serial: str, x: int, y: int, human: bool = False) -> dict:
        if human:
            ctrl = await self._ensure_scrcpy(serial)
            if ctrl:
                try:
                    await humanize.human_tap(ctrl, x, y)
                    return {"ok": True, "via": "human"}
                except Exception as e:  # noqa: BLE001
                    print(f"[human {serial}] tap failed: {e}", file=sys.stderr)
        m = await self._ensure_minitouch(serial)
        if m:
            try:
                await m.tap(x, y)
                return {"ok": True, "via": "minitouch"}
            except Exception as e:  # noqa: BLE001
                print(f"[minitouch {serial}] tap failed: {e}", file=sys.stderr)
                await self._drop_minitouch(serial)
        return await adb.tap(serial, x, y)

    async def _swipe(self, serial: str, x1: int, y1: int, x2: int, y2: int, dur: int,
                     human: bool = False) -> dict:
        if human:
            ctrl = await self._ensure_scrcpy(serial)
            if ctrl:
                try:
                    await humanize.human_swipe(ctrl, x1, y1, x2, y2,
                                               duration=(dur / 1000.0) if dur else None)
                    return {"ok": True, "via": "human"}
                except Exception as e:  # noqa: BLE001
                    print(f"[human {serial}] swipe failed: {e}", file=sys.stderr)
        m = await self._ensure_minitouch(serial)
        if m:
            try:
                await m.swipe(x1, y1, x2, y2, dur)
                return {"ok": True, "via": "minitouch"}
            except Exception as e:  # noqa: BLE001
                print(f"[minitouch {serial}] swipe failed: {e}", file=sys.stderr)
                await self._drop_minitouch(serial)
        return await adb.swipe(serial, x1, y1, x2, y2, dur)

    # -- ordered per-device input pipeline ---------------------------------
    def _enqueue_input(self, msg: dict) -> None:
        serial = msg.get("serial", "")
        q = self._input_queues.get(serial)
        if q is None:
            q = asyncio.Queue()
            self._input_queues[serial] = q
            self._input_workers[serial] = asyncio.create_task(self._input_worker(serial, q))
        q.put_nowait(msg)

    async def _input_worker(self, serial: str, q: asyncio.Queue) -> None:
        """Process one device's input events strictly in order. Consecutive
        touch_move events are coalesced to the latest so a fast drag never lags
        behind a growing backlog."""
        try:
            while True:
                msg = await q.get()
                if msg.get("action") == "touch_move":
                    # keep only the newest of a run of moves
                    while not q.empty():
                        nxt = q.get_nowait()
                        if nxt.get("action") == "touch_move":
                            msg = nxt
                        else:
                            await self.handle_input(msg)
                            msg = nxt
                            break
                await self.handle_input(msg)
        except asyncio.CancelledError:
            pass

    def _stop_input_worker(self, serial: str) -> None:
        t = self._input_workers.pop(serial, None)
        if t:
            t.cancel()
        self._input_queues.pop(serial, None)

    # -- scrcpy control (instant input) -----------------------------------
    async def _ensure_scrcpy(self, serial: str) -> scrcpycontrol.ScrcpyControl | None:
        c = self.scrcpy.get(serial)
        if c is not None:
            return c
        if serial in self._scrcpy_unavailable or not scrcpycontrol.available():
            return None
        async with self._scrcpy_start_lock:
            c = self.scrcpy.get(serial)
            if c is not None:
                return c
            if serial in self._scrcpy_unavailable:
                return None
            info = self.devices.get(serial, {})
            try:
                c = scrcpycontrol.ScrcpyControl(
                    serial, int(info.get("width", 1080)), int(info.get("height", 1920)),
                    deployed=(serial in self._scrcpy_deployed),
                )
                await c.start()
                self._scrcpy_deployed.add(serial)
                self.scrcpy[serial] = c
                print(f"[scrcpy {serial}] control active (instant input)")
                return c
            except Exception as e:  # noqa: BLE001
                if c is not None:
                    try:
                        await c.stop()  # kill the already-spawned on-device server + forward
                    except Exception:  # noqa: BLE001
                        pass
                self._scrcpy_unavailable.add(serial)
                print(f"[scrcpy {serial}] unavailable ({e}); using adb input", file=sys.stderr)
                return None

    async def _drop_scrcpy(self, serial: str) -> None:
        c = self.scrcpy.pop(serial, None)
        if c:
            await c.stop()

    async def _fast_tap(self, serial: str, x: int, y: int) -> None:
        c = await self._ensure_scrcpy(serial)
        if c:
            try:
                await c.tap(x, y)
                return
            except Exception as e:  # noqa: BLE001
                print(f"[scrcpy {serial}] tap fail ({e}); adb", file=sys.stderr)
                self._scrcpy_unavailable.add(serial)
                await self._drop_scrcpy(serial)
        await self._input_shell(serial).tap(x, y)

    async def _fast_swipe(self, serial: str, x1: int, y1: int, x2: int, y2: int) -> None:
        c = await self._ensure_scrcpy(serial)
        if c:
            try:
                await c.swipe(x1, y1, x2, y2)
                return
            except Exception as e:  # noqa: BLE001
                print(f"[scrcpy {serial}] swipe fail ({e}); adb", file=sys.stderr)
                self._scrcpy_unavailable.add(serial)
                await self._drop_scrcpy(serial)
        await self._input_shell(serial).swipe(x1, y1, x2, y2)

    def _input_shell(self, serial: str) -> deviceinput.InputShell:
        sh = self._input_shells.get(serial)
        if sh is None:
            sh = deviceinput.InputShell(serial)
            self._input_shells[serial] = sh
        return sh

    @staticmethod
    def _serial_lock(locks: dict[str, asyncio.Lock], serial: str) -> asyncio.Lock:
        """Get-or-create a per-serial asyncio.Lock in the given lock dict, so
        concurrent commands for the SAME device serialize while different
        devices stay fully concurrent."""
        lock = locks.get(serial)
        if lock is None:
            lock = locks[serial] = asyncio.Lock()
        return lock

    # -- screen recording (evidence video for recipe runs) -----------------
    REC_PATH = "/sdcard/mf_rec.mp4"

    async def _record_start(self, serial: str) -> dict:
        """Start an on-device screen recording. `screenrecord` caps at 180s per
        clip; the run stops it earlier via `_record_stop`. No root needed."""
        await self._record_stop(serial)  # clear any stale recorder
        await adb.shell(serial, f"rm -f {self.REC_PATH}")
        proc = await asyncio.create_subprocess_exec(
            adb.ADB, "-s", serial, "shell", "screenrecord",
            "--bit-rate", "3000000", "--time-limit", "180", self.REC_PATH,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        self._recorders[serial] = proc
        return {"ok": True}

    async def _record_stop(self, serial: str) -> dict:
        """Stop recording, finalize the mp4, and return it base64-encoded so the
        control plane can persist + serve it."""
        proc = self._recorders.pop(serial, None)
        if proc is None:
            return {"ok": False, "detail": "not recording"}
        # SIGINT to the on-device screenrecord makes it write a valid moov atom
        await adb.shell(serial, "kill -INT $(pidof screenrecord) 2>/dev/null")
        await asyncio.sleep(1.5)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        rc, out, _ = await adb._run(["-s", serial, "exec-out", "cat", self.REC_PATH], timeout=60)
        await adb.shell(serial, f"rm -f {self.REC_PATH}")
        if rc != 0 or not out:
            return {"ok": False, "detail": "no recording captured"}
        return {"ok": True, "mp4_b64": base64.b64encode(out).decode("ascii")}

    # -- fast fire-and-forget input (runs on the ordered worker) -----------
    async def handle_input(self, msg: dict) -> None:
        serial = msg["serial"]
        action = msg.get("action")
        p = msg.get("payload", {})
        try:
            if action == "tap":
                await self._fast_tap(serial, int(p["x"]), int(p["y"]))
            elif action == "swipe":
                await self._fast_swipe(
                    serial, int(p["x1"]), int(p["y1"]), int(p["x2"]), int(p["y2"])
                )
            elif action in ("touch_down", "touch_move", "touch_up"):
                await self._touch(serial, action, p)
            elif action == "long_press":
                await self._long_press(serial, int(p["x"]), int(p["y"]),
                                       int(p.get("duration_ms", 600)))
            elif action == "text":
                await self._input_shell(serial).text(p["text"])
            elif action == "key":
                await self._input_shell(serial).key(str(p["keycode"]))
        except Exception as e:  # noqa: BLE001
            print(f"[input {serial}] {action} failed: {e}", file=sys.stderr)

    async def _touch(self, serial: str, action: str, p: dict) -> None:
        """Touch handling. Primary: scrcpy control = instant real-time finger
        tracking (down/move/up injected in ~1ms). Fallback (scrcpy unavailable):
        buffer the gesture and emit ONE adb tap/swipe on release — never a JVM
        per move."""
        x, y = int(p.get("x", 0)), int(p.get("y", 0))

        if action == "touch_down":
            # Track the gesture origin regardless of which path ends up handling
            # it, so a mid-gesture scrcpy failure on a later touch_move/touch_up
            # can still fall back to the adb path with the correct start coords.
            self._touch_start[serial] = (x, y)

        c = await self._ensure_scrcpy(serial)
        if c:
            try:
                if action == "touch_down":
                    await c.down(x, y)
                elif action == "touch_move":
                    await c.move(x, y)
                elif action == "touch_up":
                    await c.up(x, y)
                    self._touch_start.pop(serial, None)
                return
            except Exception as e:  # noqa: BLE001
                print(f"[scrcpy {serial}] touch fail ({e}); adb", file=sys.stderr)
                self._scrcpy_unavailable.add(serial)
                await self._drop_scrcpy(serial)
                # fall through to the adb gesture path

        # adb-input gesture fallback (buffer → one tap/swipe/long-press on release)
        # (_touch_start is already set above, for both this path and the scrcpy path)
        if action == "touch_down":
            self._touch_last[serial] = (x, y)
            self._touch_t[serial] = asyncio.get_running_loop().time()
        elif action == "touch_move":
            self._touch_last[serial] = (x, y)
        elif action == "touch_up":
            sx, sy = self._touch_start.pop(serial, (x, y))
            self._touch_last.pop(serial, None)
            t0 = self._touch_t.pop(serial, None)
            held_ms = int((asyncio.get_running_loop().time() - t0) * 1000) if t0 else 0
            dist = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
            sh = self._input_shell(serial)
            if dist < 24 and held_ms >= 450:
                # pressed and held in place → long-press (swipe in place w/ duration)
                await sh.swipe(sx, sy, sx, sy, max(held_ms, 500))
            elif dist < 24:
                await sh.tap(sx, sy)
            else:
                await sh.swipe(sx, sy, x, y, 140)

    async def _long_press(self, serial: str, x: int, y: int, dur_ms: int) -> None:
        """Explicit press-and-hold at (x,y). Real finger held for dur_ms so
        Android fires its long-press (context menu: paste / select / etc.).
        scrcpy = hold the injected finger; adb fallback = swipe-in-place."""
        dur_ms = max(300, min(dur_ms, 5000))
        c = await self._ensure_scrcpy(serial)
        if c:
            try:
                await c.down(x, y)
                await asyncio.sleep(dur_ms / 1000.0)
                await c.up(x, y)
                return
            except Exception as e:  # noqa: BLE001
                print(f"[scrcpy {serial}] long_press fail ({e}); adb", file=sys.stderr)
                self._scrcpy_unavailable.add(serial)
                await self._drop_scrcpy(serial)
        await self._input_shell(serial).swipe(x, y, x, y, dur_ms)

    # -- command dispatch --------------------------------------------------
    async def handle_command(self, msg: dict) -> None:
        serial = msg.get("serial", "")
        action = msg.get("action")
        payload = msg.get("payload", {})
        request_id = msg.get("request_id")
        result: dict

        try:
            if action == "shell":
                result = await adb.shell(serial, payload["cmd"])
            elif action == "install":
                result = await adb.install(serial, payload["apk_path"])
            elif action == "clone_package":
                # Copy an installed package's APK(s) from THIS device onto others
                # (e.g. clone a modern WebView onto devices stuck on an old one).
                # No download — it's the source device's own Google-signed binary.
                result = await adb.clone_package(serial, payload.get("to_serials", []),
                                                 payload["package"], timeout=float(payload.get("timeout", 600)))
            elif action == "tap":
                result = await self._tap(serial, int(payload["x"]), int(payload["y"]),
                                         human=bool(payload.get("human", False)))
            elif action == "swipe":
                result = await self._swipe(
                    serial,
                    int(payload["x1"]), int(payload["y1"]),
                    int(payload["x2"]), int(payload["y2"]),
                    int(payload.get("duration_ms", 200)),
                    human=bool(payload.get("human", False)),
                )
            elif action == "long_press":
                await self._long_press(serial, int(payload["x"]), int(payload["y"]),
                                       int(payload.get("duration_ms", 600)))
                result = {"ok": True}
            elif action == "text":
                result = await adb.input_text(serial, payload["text"])
            elif action == "human_type":
                result = await humanize.human_type(serial, payload["text"],
                                                   typo_rate=float(payload.get("typo_rate", 0.04)))
            elif action == "human_scroll":
                ctrl = await self._ensure_scrcpy(serial)
                if ctrl is None:
                    result = {"ok": False, "error": "scrcpy control unavailable for human_scroll"}
                else:
                    await humanize.human_scroll(ctrl, ctrl.width, ctrl.height,
                                                direction=payload.get("direction", "up"),
                                                amount=float(payload.get("amount", 1.0)))
                    result = {"ok": True}
            elif action == "watch_feed":
                ctrl = await self._ensure_scrcpy(serial)
                if ctrl is None:
                    result = {"ok": False, "error": "scrcpy control unavailable for watch_feed"}
                else:
                    result = await humanize.watch_feed(
                        ctrl, ctrl.width, ctrl.height,
                        videos=int(payload.get("videos", 5)),
                        min_watch=float(payload.get("min_watch", 8)),
                        max_watch=float(payload.get("max_watch", 45)),
                    )
            elif action == "youtube":
                # Human-like YouTube flows (watch by link / search / channel / shorts).
                ctrl = await self._ensure_scrcpy(serial)
                if ctrl is None:
                    result = {"ok": False, "error": "scrcpy control unavailable for youtube"}
                else:
                    result = await youtube.run(serial, ctrl, payload)
            elif action == "key":
                result = await adb.keyevent(serial, str(payload["keycode"]))
            elif action == "reboot":
                result = await adb.reboot(serial)
            elif action == "restore_defaults":
                # Clean-slate a device: auto-rotate off, home, recents cleared, etc.
                result = await adb.restore_defaults(serial)
            elif action == "set_orientation":
                # Lock the display: portrait | landscape | auto.
                result = await adb.set_orientation(serial, payload.get("mode", "portrait"))
            elif action == "get_orientation":
                # Read-only: current rotation lock state, no side effects.
                result = await adb.orientation_status(serial)
            elif action == "logcat":
                result = await adb.logcat_dump(serial, payload.get("lines", 400))
            elif action == "connect_wifi":
                # Agent-level action — no existing device serial yet, that's
                # the point (this is how a device is ADDED over the network).
                result = await adb.connect_wifi(payload["host"], int(payload.get("port", 5555)))
            elif action == "disconnect_wifi":
                result = await adb.disconnect_wifi(payload["host"], int(payload.get("port", 5555)))
            elif action == "enable_tcpip":
                result = await adb.enable_tcpip(serial, int(payload.get("port", 5555)))
            elif action == "preflight":
                # Readiness checklist before running automation.
                result = await preflight.check(serial)
            elif action == "remove_blackbox":
                result = await appsetup.remove_blackbox(serial)
            elif action == "install_required":
                result = await appsetup.install_required(serial)
            elif action == "required_status":
                result = await appsetup.required_status(serial)
            elif action == "set_proxy":
                result = await adb.set_proxy(serial, payload["host"], int(payload["port"]))
            elif action == "clear_proxy":
                result = await adb.clear_proxy(serial)
            elif action == "get_proxy":
                result = await adb.get_proxy(serial)
            elif action == "spoof_fingerprint":
                result = await fingerprint_spoofer.spoof_all(
                    serial,
                    profile_name=payload.get("profile_name"),
                    custom_settings=payload.get("custom_settings")
                )
            elif action == "rollback_fingerprint":
                result = await fingerprint_spoofer.rollback(serial)
            elif action == "fingerprint_status":
                result = await fingerprint_spoofer.get_status(serial)
            # ---- Google account sign-in/out ------------------------------------
            elif action == "google_signin":
                result = await googleaccounts.sign_in(
                    serial, payload["email"], payload["password"],
                    timeout=float(payload.get("timeout", 120)),
                    vision_model=payload.get("vision_model"), recovery_model=payload.get("recovery_model"))
            elif action == "google_signout":
                result = await googleaccounts.sign_out(
                    serial, payload.get("email"), timeout=float(payload.get("timeout", 60)))
            elif action == "google_list_accounts":
                result = await googleaccounts.list_accounts(serial)
            # ---- Play Store -----------------------------------------------------
            elif action == "playstore_install":
                result = await playstore.install_app(
                    serial, payload["package"], timeout=float(payload.get("timeout", 180)))
            # ---- Settings ---------------------------------------------------------
            elif action == "settings_open":
                result = await settingsauto.open_page(serial, payload.get("page", "main"))
            elif action == "settings_open_app":
                result = await settingsauto.open_app_settings(serial, payload["package"])
            elif action == "settings_wifi":
                result = await settingsauto.set_wifi(serial, bool(payload.get("enabled", True)))
            elif action == "settings_airplane_mode":
                result = await settingsauto.set_airplane_mode(serial, bool(payload.get("enabled", True)))
            elif action == "settings_clear_app_data":
                result = await settingsauto.clear_app_data(serial, payload["package"])
            elif action == "settings_app_permission":
                result = await settingsauto.set_app_permission(
                    serial, payload["package"], payload["permission"], bool(payload.get("grant", True)))
            # ---- leak-proof full-tunnel proxy (Clash Meta) --------------------
            elif action == "tunnel_onboard":
                async with self._serial_lock(self._tunnel_locks, serial):
                    self._tunnel_busy.add(serial)
                    try:
                        result = await clashtunnel.onboard(serial, payload["proxy"])
                    finally:
                        self._tunnel_busy.discard(serial)
            elif action == "tunnel_set_proxy":
                async with self._serial_lock(self._tunnel_locks, serial):
                    self._tunnel_busy.add(serial)
                    try:
                        result = await clashtunnel.set_proxy(serial, payload["proxy"])
                    finally:
                        self._tunnel_busy.discard(serial)
            elif action == "tunnel_verify":
                result = await clashtunnel.verify_exit(serial)
            elif action == "tunnel_status":
                result = await clashtunnel.status(serial)
            elif action == "tunnel_heal":
                # revive a fail-closed device (core died) without a reboot
                async with self._serial_lock(self._tunnel_locks, serial):
                    self._tunnel_busy.add(serial)
                    try:
                        result = await clashtunnel.heal(serial)
                    finally:
                        self._tunnel_busy.discard(serial)
            elif action == "tunnel_start":
                result = await clashtunnel.start(serial)
            elif action == "tunnel_stop":
                result = await clashtunnel.stop(serial)
            elif action == "tunnel_disable":
                # disable removes the kill-switch → not fail-closed; suppress the
                # watchdog briefly so it doesn't re-heal a device being torn down.
                async with self._serial_lock(self._tunnel_locks, serial):
                    self._tunnel_busy.add(serial)
                    try:
                        result = await clashtunnel.disable(serial)
                    finally:
                        self._tunnel_busy.discard(serial)
            elif action == "emu":
                result = await adb.emu(serial, payload["args"])
            elif action == "screenshot":
                jpg = await adb.screencap_full_jpeg(serial, int(payload.get("quality", 85)))
                if jpg is None:
                    result = {"ok": False, "error": "screencap failed"}
                else:
                    result = {"ok": True, "jpeg_b64": base64.b64encode(jpg).decode("ascii")}
            elif action == "run_script":
                result = await scripting.run_script(
                    serial,
                    payload.get("interpreter", "python"),
                    payload["code"],
                    float(payload.get("timeout", 30)),
                )
            elif action == "list_apps":
                result = await adb.list_apps(serial, payload.get("third_party", True))
            elif action == "launch_package":
                result = await adb.launch_package(serial, payload["package"])
            elif action == "force_stop":
                result = await adb.force_stop(serial, payload["package"])
            elif action == "droidrun_task":
                # AI-driven UI automation from a natural-language goal (mobilerun+Ollama).
                result = await droidrun.run_task(
                    serial,
                    payload["task"],
                    provider=payload.get("provider", droidrun.DEFAULT_PROVIDER),
                    model=payload.get("model", droidrun.DEFAULT_MODEL),
                    base_url=payload.get("base_url", droidrun.DEFAULT_BASE_URL),
                    steps=int(payload.get("steps", droidrun.DEFAULT_STEPS)),
                    vision=bool(payload.get("vision", False)),
                    reasoning=bool(payload.get("reasoning", False)),
                    timeout=float(payload.get("timeout", 600)),
                )
            elif action == "droidrun_setup":
                result = await droidrun.setup(serial)
            elif action == "droidrun_ping":
                result = await droidrun.ping(serial)
            elif action == "mobilerun_status":
                result = await droidrun.status()
            elif action == "detect_screen":
                # Vision-LLM screen-state classifier (normal/captcha/blocked/...).
                result = await detect.classify_screen(
                    serial,
                    model=payload.get("model", detect.DEFAULT_VISION_MODEL),
                    timeout=float(payload.get("timeout", 150)),
                )
            elif action == "macro_record":
                result = await droidrun.record_macro(
                    serial, payload["task"], payload["name"],
                    provider=payload.get("provider", droidrun.DEFAULT_PROVIDER),
                    model=payload.get("model", droidrun.DEFAULT_MODEL),
                    base_url=payload.get("base_url", droidrun.DEFAULT_BASE_URL),
                    steps=int(payload.get("steps", droidrun.DEFAULT_STEPS)),
                    vision=bool(payload.get("vision", False)),
                    timeout=float(payload.get("timeout", 600)))
            elif action == "macro_replay":
                result = await droidrun.replay_macro(
                    serial, payload["name"],
                    delay=float(payload.get("delay", 0.6)),
                    on_mismatch=payload.get("on_mismatch", "agent"),
                    provider=payload.get("provider", droidrun.DEFAULT_PROVIDER),
                    model=payload.get("model", droidrun.DEFAULT_MODEL),
                    base_url=payload.get("base_url", droidrun.DEFAULT_BASE_URL),
                    timeout=float(payload.get("timeout", 400)))
            elif action == "macro_list":
                result = droidrun.list_macros()
            elif action == "ui_state":
                # Fast on-screen UI query for recipe watchers (present + tap center).
                result = await recipeui.ui_state(serial, payload.get("queries", []))
            elif action == "match_template":
                # Image/template match on the current screen (icons w/o text).
                result = await recipeui.match_template(
                    serial, payload["template_b64"], float(payload.get("threshold", 0.82)))
            elif action == "ocr_read":
                # No-LLM OCR: read all on-screen text (pixels the a11y tree misses).
                result = {"ok": True, "regions": await vision.ocr_read(
                    serial, payload.get("region"))}
            elif action == "ocr_find":
                hit = await vision.ocr_find(serial, payload["text"], payload.get("region"))
                result = {"ok": bool(hit), "present": bool(hit), **(hit or {})}
            elif action == "ocr_tap":
                result = await vision.ocr_tap(serial, payload["text"], payload.get("region"))
            elif action == "ocr_wait_for":
                result = await vision.ocr_wait_for(
                    serial, payload["text"], float(payload.get("timeout", 15)), payload.get("region"))
            elif action == "record_start":
                async with self._serial_lock(self._record_locks, serial):
                    result = await self._record_start(serial)
            elif action == "record_stop":
                async with self._serial_lock(self._record_locks, serial):
                    result = await self._record_stop(serial)
            elif action == "device_setup":
                # One-click onboarding: install the AI Portal + device prep.
                result = await onboard.setup_device(serial, payload.get("options", {}))
            elif action == "start_mirror":
                self._start_mirror(serial, payload.get("mode", "h264"))
                result = {"ok": True}
            elif action == "stop_mirror":
                self._stop_mirror(serial)
                result = {"ok": True}
            # ---- self-update (agent-level, not tied to any device) --------------
            elif action == "agent_version":
                result = {"ok": True, "version": VERSION}
            elif action == "self_update":
                result = await selfupdate.apply_update(payload["bundle_b64"], payload.get("version", "unknown"))
                if result.get("ok"):
                    selfupdate.schedule_restart()
            elif action == "self_rollback":
                result = await selfupdate.rollback()
                if result.get("ok"):
                    selfupdate.schedule_restart()
            elif action == "github_check_update":
                result = await githubupdate.check_latest()
            elif action == "github_apply_update":
                result = await githubupdate.apply_latest()
                if result.get("ok"):
                    selfupdate.schedule_restart()
            # ---- local Ollama model management (agent-level) --------------------
            elif action == "ollama_list_models":
                result = await ollamaadmin.list_models()
            elif action == "ollama_pull_model":
                result = await ollamaadmin.pull_model(payload["tag"])
            elif action == "ollama_pull_status":
                result = await ollamaadmin.pull_status(payload["tag"])
            elif action == "ollama_delete_model":
                result = await ollamaadmin.delete_model(payload["tag"])
            else:
                result = {"ok": False, "error": f"unknown action {action}"}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "error": str(e)}

        await self._send({"type": "result", "request_id": request_id, "result": result})

    # -- socket helpers ----------------------------------------------------
    async def _send(self, obj: dict) -> None:
        if self.ws:
            payload = json.dumps(obj)
            async with self._send_lock:
                await self.ws.send(payload)
            self.bytes_sent += len(payload)

    async def _broadcast(self, obj: dict) -> None:
        """Like _send, but also fans out to every read-only observer (see
        self.observers) — for informational messages (register/devices/
        heartbeat/crash_event) that every connected party should see, as
        opposed to a command `result`, which only ever goes back to the
        primary connection that issued the command (observers can't issue
        commands in the first place — see _serve_observer)."""
        await self._send(obj)
        if not self.observers:
            return
        payload = json.dumps(obj)
        for info in list(self.observers.values()):
            try:
                async with info["lock"]:
                    await info["ws"].send(payload)
            except Exception:  # noqa: BLE001
                pass  # a dead/dropping observer socket cleans itself up in _serve_observer

    async def _send_frame(self, serial: str, jpg: bytes) -> None:
        """Send a mirror frame as a BINARY ws message (no base64/JSON overhead).
        Wire format: 0x01 | len(serial) | serial | jpeg-bytes.
        """
        if not self.ws:
            return
        sb = serial.encode()
        packet = bytes([0x01, len(sb)]) + sb + jpg
        async with self._send_lock:
            await self.ws.send(packet)
        self.bytes_sent += len(packet)

    async def _register(self) -> None:
        await self.refresh_devices()
        visible = self._visible_devices()
        await self._broadcast(
            {"type": "register", "agent_id": self.agent_id, "devices": visible, "version": VERSION}
        )
        print(f"registered agent '{self.agent_id}' with {len(visible)} device(s)"
              + (f" ({len(self.devices) - len(visible)} hidden)" if len(visible) != len(self.devices) else ""))

    def _load_snapshot(self) -> dict:
        """Cheap, cross-platform signal for 'how loaded is this host' — see
        the module docstring. Per-device task counts are the primary number
        (always available); system load average / memory pressure are a
        secondary signal added where the OS exposes them (Linux/most edge
        hosts — silently omitted elsewhere, e.g. Windows desktop agents)."""
        snap: dict = {
            "device_count": len(self.devices),
            "active_mirrors": len(self.mirror_tasks),
            "active_scrcpy": len(self.scrcpy),
            "active_input_workers": len(self._input_workers),
        }
        try:
            snap["loadavg_1m"] = round(os.getloadavg()[0], 2)
        except (AttributeError, OSError):
            pass
        try:
            meminfo: dict[str, int] = {}
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    meminfo[k.strip()] = int(v.strip().split()[0])  # kB
            if "MemTotal" in meminfo and "MemAvailable" in meminfo and meminfo["MemTotal"]:
                snap["mem_used_pct"] = round(100 * (1 - meminfo["MemAvailable"] / meminfo["MemTotal"]), 1)
        except (OSError, ValueError, KeyError, IndexError):
            pass
        return snap

    async def _heartbeat_loop(self) -> None:
        """Dedicated liveness ping. Kept separate from device polling so a long
        device command (e.g. a multi-minute YouTube flow hammering `uiautomator
        dump`) can never starve the heartbeat and get the agent reaped."""
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            try:
                await self._broadcast({"type": "heartbeat", "load": self._load_snapshot()})
            except Exception:  # noqa: BLE001
                return

    async def _tunnel_watchdog(self) -> None:
        """Keep protected devices from silently going dark. If the kill-switch is
        armed but the tunnel is down, the Clash core has died and the device is
        fail-closed (no internet) — restart the core. Requires two consecutive
        fail-closed observations before acting, so it never fights the transient
        down-states during onboard/reboot. Devices under an active tunnel op are
        skipped entirely (self._tunnel_busy)."""
        fail_streak: dict[str, int] = {}
        while True:
            await asyncio.sleep(WATCHDOG_SEC)
            for serial in list(self.devices):
                if serial in self._tunnel_busy:
                    fail_streak.pop(serial, None)
                    continue
                try:
                    # cheap first check: a live tunnel clears the streak immediately
                    if await clashtunnel.tun_up(serial):
                        fail_streak.pop(serial, None)
                        continue
                    # tun down — only a problem if the kill-switch is armed
                    if not await clashtunnel.is_lockdown_armed(serial):
                        fail_streak.pop(serial, None)
                        continue
                    fail_streak[serial] = fail_streak.get(serial, 0) + 1
                    if fail_streak[serial] < 2:
                        continue  # rule out a transient before acting
                    print(f"[watchdog {serial}] fail-closed (core down, lockdown armed) — healing", flush=True)
                    res = await clashtunnel.heal(serial)
                    if res.get("healed"):
                        print(f"[watchdog {serial}] tunnel recovered ({res.get('action')})", flush=True)
                        fail_streak.pop(serial, None)
                    else:
                        print(f"[watchdog {serial}] heal did not restore tunnel: {res}",
                              file=sys.stderr, flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[watchdog {serial}] error: {e}", file=sys.stderr, flush=True)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_DEVICES_SEC)
            # Watchdog: unstick devices that dropped to 'offline' (common on the
            # bare-board bench) so they rejoin without manual intervention.
            try:
                await adb.reconnect_offline()
            except Exception:  # noqa: BLE001
                pass
            try:
                changed = await self.refresh_devices()
            except Exception as e:  # noqa: BLE001
                print(f"device poll error: {e}", file=sys.stderr)
                continue
            if changed:
                await self._broadcast({"type": "devices", "devices": self._visible_devices()})

    async def push_devices_update(self) -> None:
        """Re-broadcast the current visible-device set immediately — used when
        the admin panel hides/shows a device, so the dashboard doesn't have to
        wait for the next poll tick to notice."""
        await self._broadcast({"type": "devices", "devices": self._visible_devices()})

    # -- shared per-connection body -----------------------------------------
    # Everything from here down doesn't care WHO opened the TCP connection —
    # a local agent dials OUT to the control plane (client mode, `run()`); a
    # remote agent (e.g. a Raspberry Pi running phones over its own USB) can
    # instead listen and let the control plane dial IN (server mode,
    # `listen()`). Either way, once a `websockets` connection exists, this
    # same register → poll/heartbeat/watchdog → command-dispatch loop drives
    # it — so a remote agent gets 100% feature parity for free, with zero
    # separate code path.
    async def _serve_connection(self, ws) -> None:
        self.ws = ws
        self._ws_closed.clear()
        self.bytes_sent = 0
        self.bytes_received = 0
        poller = heartbeat = watchdog = None
        try:
            # _register (real device enumeration + a network send) used to
            # run before this try/finally — if it ever raised, self.ws/
            # _ws_closed never got reset at all, permanently stranding the
            # agent unable to accept any future connection short of a manual
            # process restart. Everything that can fail is inside the block
            # now so the finally below always runs.
            await self._register()
            poller = asyncio.create_task(self._poll_loop())
            heartbeat = asyncio.create_task(self._heartbeat_loop())
            watchdog = asyncio.create_task(self._tunnel_watchdog())
            async for raw in ws:
                self.bytes_received += len(raw) if isinstance(raw, (bytes, bytearray)) else len(raw.encode("utf-8", "ignore"))
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "command":
                    asyncio.create_task(self.handle_command(msg))
                elif mtype == "input":
                    self._enqueue_input(msg)
        finally:
            tasks = [t for t in (poller, heartbeat, watchdog) if t is not None]
            for t in tasks:
                t.cancel()
            # Wait for them to actually unwind before a new connection can start
            # (self.ws is cleared below) — otherwise a fresh connection's own
            # poller/heartbeat/watchdog could start touching the same shared
            # state concurrently with the old ones still stopping.
            await asyncio.gather(*tasks, return_exceptions=True)
            for s in list(self.mirror_tasks):
                self._stop_mirror(s)
            for s in list(self._input_workers):
                self._stop_input_worker(s)
            for s in list(self._input_shells):
                await self._input_shells.pop(s).stop()
            for s in list(self.scrcpy):
                await self._drop_scrcpy(s)
            for s in list(self.devices):
                crashmonitor.stop(s)
            for s in list(self.minitouch):
                await self._drop_minitouch(s)
            self.ws = None
            self.current_peer = None
            self._ws_closed.set()

    # -- server mode only: an additional, read-only connection --------------
    async def _serve_observer(self, ws, connection_id: str) -> None:
        """A connection beyond the primary (self.ws) — e.g. a second dashboard
        that just wants to watch this box's fleet status. Gets the same
        register/devices/heartbeat/crash_event feed as the primary (see
        _broadcast), but any command/input it sends back is refused rather
        than dispatched — only the primary drives the physical device, so two
        connections can never race a tap/swipe against each other."""
        try:
            await ws.send(json.dumps({
                "type": "register", "agent_id": self.agent_id,
                "devices": self._visible_devices(), "version": VERSION,
            }))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if msg.get("type") in ("command", "input"):
                    await ws.send(json.dumps({
                        "type": "result", "request_id": msg.get("request_id"),
                        "result": {"ok": False, "error": "read-only connection — another connection is this agent's active controller"},
                    }))
        finally:
            if self.observers.get(connection_id, {}).get("ws") is ws:
                self.observers.pop(connection_id, None)

    # -- client mode: dial OUT to a control plane, with reconnect -----------
    async def run(self) -> None:
        while True:
            try:
                async with websockets.connect(self.backend_url, max_size=None) as ws:
                    await self._serve_connection(ws)
            except Exception as e:  # noqa: BLE001
                print(f"connection lost ({e}); retrying in 3s", file=sys.stderr)
            self.ws = None
            await asyncio.sleep(3)

    async def kick(self) -> bool:
        """Force-disconnect whoever is currently connected, without banning
        them — used by the admin panel's "Disconnect" action. The dashboard
        client, if this was a legitimate one, will just reconnect."""
        if self.ws is None:
            return False
        try:
            await self.ws.close(code=4005, reason="disconnected by operator")
        except Exception:  # noqa: BLE001
            pass
        return True

    async def kick_observer(self, connection_id: str) -> bool:
        """Force-disconnect one read-only observer connection by id, without
        touching the primary or any other observer."""
        info = self.observers.get(connection_id)
        if info is None:
            return False
        try:
            await info["ws"].close(code=4005, reason="disconnected by operator")
        except Exception:  # noqa: BLE001
            pass
        return True

    def _is_same_connection(self, matched_id) -> bool:
        """True when `matched_id` (a newly-authenticated connection's own
        registered id) is the same registered connection currently occupying
        self.ws — i.e. a reconnect, not a genuinely different second client."""
        return bool(self.current_peer) and self.current_peer.get("connection_id") == matched_id

    # -- server mode: the control plane dials IN, token-authenticated -------
    async def listen(self, host: str, port: int, check_token, ban_store, get_allow_remote) -> None:
        """`check_token(token, peer_ip, peer_scope) -> (ok, reason, connection)`
        validates against whatever connections/tokens store the caller manages
        (see connections.py) — `connection` is {"id","name"} of the matched
        row on success. `ban_store` is a bans.BanStore. `get_allow_remote()`
        returns the global remote-access switch (see agentsettings.py)."""
        import netinfo

        async def handler(ws) -> None:
            peer_ip = ws.remote_address[0] if ws.remote_address else None
            if peer_ip and ban_store.is_banned(peer_ip):
                print(f"listen(): rejected a banned IP ({peer_ip})", file=sys.stderr)
                self._log_connection_attempt(peer_ip, "?", "rejected", "banned address")
                await ws.close(code=4003, reason="this address is banned")
                return
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                auth = json.loads(raw)
            except Exception:  # noqa: BLE001
                self._log_connection_attempt(peer_ip, "?", "rejected", "no auth message")
                await ws.close(code=4000, reason="expected an auth message")
                return
            if auth.get("type") != "auth":
                self._log_connection_attempt(peer_ip, "?", "rejected", "invalid token")
                await ws.close(code=4001, reason="invalid token")
                return
            peer_scope = netinfo.classify_peer(peer_ip)
            if peer_scope == "remote" and not get_allow_remote():
                print(f"listen(): rejected a remote connection from {peer_ip} — remote access is disabled", file=sys.stderr)
                self._log_connection_attempt(peer_ip, peer_scope, "rejected", "remote access disabled")
                await ws.close(code=4004, reason="remote (outside-LAN) connections are currently disabled on this agent")
                return
            ok, reason, matched = check_token(auth.get("token", ""), peer_ip, peer_scope)
            if not ok:
                print(f"listen(): rejected a connection from {peer_ip} ({reason})", file=sys.stderr)
                self._log_connection_attempt(peer_ip, peer_scope, "rejected", reason)
                await ws.close(code=4001, reason=reason or "invalid token")
                return
            # This single Agent instance (mirror tasks, minitouch/scrcpy
            # sessions, self.ws) can only be DRIVEN (commands/input) by ONE
            # connection at a time — two connections independently tapping/
            # swiping the same physical screen would race each other with no
            # way to tell whose gesture is whose. That one connection is the
            # "primary" (self.ws / self.current_peer, exactly as before).
            #
            # A second, genuinely different token connecting while a primary
            # is already active is no longer rejected outright — it's
            # accepted as a read-only "observer" (self.observers): it gets
            # the same register/devices/heartbeat feed as the primary (see
            # _broadcast) but any command/input it sends back is refused (see
            # _serve_observer), so it can never race the primary on the
            # device. This is the common case for e.g. a second dashboard
            # that just wants to watch this box's fleet status.
            #
            # The one case that's still handled specially is the SAME
            # registered connection reconnecting (matched["id"] == the
            # currently-active primary's) — the common case after this
            # agent's own self-update restart or a backend restart: the old
            # TCP session can go half-open (no clean close ever arrives) and
            # self.ws stays set to a connection that's actually dead,
            # otherwise stranding every future legitimate reconnect attempt
            # behind it. Superseding it — closing the stale socket and
            # waiting for its cleanup before accepting the new one — actually
            # confirmed live: a push left the control plane stuck for minutes
            # straight with no way to recover short of restarting the agent
            # process by hand.
            if self.ws is not None and self._is_same_connection(matched["id"]):
                print(f"listen(): superseding a stale connection from '{matched['name']}' with a fresh one", file=sys.stderr)
                stale_ws = self.ws
                try:
                    await stale_ws.close(code=4006, reason="superseded by a new connection")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await asyncio.wait_for(self._ws_closed.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    print("listen(): stale connection didn't clean up in time — rejecting the new one", file=sys.stderr)
                    self._log_connection_attempt(peer_ip, peer_scope, "rejected", "stale connection cleanup timed out", matched["name"])
                    await ws.close(code=4002, reason="already connected to a control plane")
                    return
            elif self.ws is not None:
                # A different token while a primary is active → read-only observer.
                # Reconnecting under the same observer token replaces its own
                # stale entry rather than piling up duplicate sockets for it.
                stale = self.observers.get(matched["id"])
                if stale is not None:
                    try:
                        await stale["ws"].close(code=4006, reason="superseded by a new connection")
                    except Exception:  # noqa: BLE001
                        pass
                print(f"listen(): accepted an additional read-only connection from {peer_ip} ({peer_scope}) via '{matched['name']}'")
                self._log_connection_attempt(peer_ip, peer_scope, "accepted (read-only)", None, matched["name"])
                self.observers[matched["id"]] = {
                    "ws": ws, "lock": asyncio.Lock(), "ip": peer_ip, "scope": peer_scope,
                    "connection_name": matched["name"], "connected_since": time.time(),
                }
                try:
                    await self._serve_observer(ws, matched["id"])
                except Exception as e:  # noqa: BLE001
                    print(f"listen(): observer connection ended ({e})", file=sys.stderr)
                return
            print(f"listen(): accepted an authenticated control-plane connection from {peer_ip} ({peer_scope}) via '{matched['name']}'")
            self._log_connection_attempt(peer_ip, peer_scope, "accepted", None, matched["name"])
            self.current_peer = {
                "ip": peer_ip, "scope": peer_scope, "connected_since": time.time(),
                "connection_id": matched["id"], "connection_name": matched["name"],
            }
            try:
                await self._serve_connection(ws)
            except Exception as e:  # noqa: BLE001
                print(f"listen(): connection ended ({e})", file=sys.stderr)
            finally:
                self.current_peer = None

        # ping_interval/ping_timeout: proactively detect a half-open TCP
        # session (peer vanished without a clean close — common over NAT'd/
        # VPN-routed links) so self.ws gets cleared on its own well before a
        # reconnect attempt ever needs the supersede path above.
        async with websockets.serve(handler, host, port, max_size=None, ping_interval=20, ping_timeout=20):
            print(f"listen(): agent listening on ws://{host}:{port}")
            await asyncio.Future()  # run forever


def main() -> None:
    parser = argparse.ArgumentParser(description="Mobile Farming node agent")
    parser.add_argument(
        "--backend", default=None, help="control-plane agent WS URL (client mode — dial OUT)"
    )
    parser.add_argument(
        "--listen", default=None,
        help="host:port to listen on instead (server mode — the control plane dials IN, "
             "token-authenticated via --connections-file)",
    )
    parser.add_argument(
        "--connections-file", default=None,
        help="JSON connections/token store for --listen mode (see connections.py); "
             "defaults to data/connections.json next to this script",
    )
    parser.add_argument(
        "--admin-port", type=int, default=8090,
        help="local admin panel port for --listen mode (connections/tokens management)",
    )
    parser.add_argument(
        "--id", default=socket.gethostname(), help="stable agent id (defaults to hostname)"
    )
    args = parser.parse_args()

    if args.listen:
        import connections as connections_mod
        import bans as bans_mod
        import agentsettings
        import devicevisibility
        import adminpanel
        import uvicorn

        host, _, port_s = args.listen.partition(":")
        listen_host, listen_port = (host or "0.0.0.0"), int(port_s or 8091)
        store = connections_mod.ConnectionStore(args.connections_file)
        ban_store = bans_mod.BanStore()
        settings_store = agentsettings.SettingsStore()
        agent = Agent(f"ws://{args.listen}", args.id)
        agent.hidden_store = devicevisibility.HiddenDevicesStore()

        admin_app = adminpanel.create_app(store, agent, listen_host, listen_port, ban_store, settings_store)
        admin_server = uvicorn.Server(uvicorn.Config(admin_app, host="0.0.0.0", port=args.admin_port, log_level="warning"))

        async def _run_both() -> None:
            await asyncio.gather(
                agent.listen(
                    listen_host, listen_port, store.check_token, ban_store,
                    lambda: settings_store.get_all()["allow_remote"],
                ),
                admin_server.serve(),
            )

        try:
            asyncio.run(_run_both())
        except KeyboardInterrupt:
            print("\nagent stopped")
        return

    agent = Agent(args.backend or "ws://localhost:8000/ws/agent", args.id)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\nagent stopped")


if __name__ == "__main__":
    main()

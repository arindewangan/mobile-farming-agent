# Vendored device binaries

These are prebuilt native helpers from the **DeviceFarmer** project, pushed to
devices over adb by the agent (no root required):

- `minicap`, `minicap.so` — fast (~30-55fps) JPEG screen capture.
  Source: https://github.com/DeviceFarmer/minicap  (Apache-2.0)
  Prebuilt: `@devicefarmer/minicap-prebuilt` (npm)
- `minitouch` — low-latency multitouch input injection (needs root/touch-node
  access; OFF by default, see `ENABLE_MINITOUCH`).
  Source: https://github.com/DeviceFarmer/minitouch  (Apache-2.0)
  Prebuilt: `@devicefarmer/minitouch-prebuilt` (npm)
- `scrcpy-server.jar` — **instant rootless input** via a persistent InputManager
  server (control-only). This is the primary touch path.
  Source: https://github.com/Genymobile/scrcpy release v2.4  (Apache-2.0)
  Download: github.com/Genymobile/scrcpy/releases/download/v2.4/scrcpy-server-v2.4

## Layout
```
vendor/<abi>/minicap        # native executable (per ABI)
vendor/<abi>/minicap.so     # capture lib (built for a specific Android SDK)
vendor/<abi>/minitouch      # native executable (per ABI)
```
Currently vendored: **arm64-v8a** (minicap.so built for **android-28**), which
matches the Samsung SM-G9500 (Galaxy S8, Android 9) fleet.

## Adding more device types
minicap's `.so` is SDK-specific. To support other ABIs / Android versions, pull
the matching artifacts:
```bash
npm pack @devicefarmer/minicap-prebuilt @devicefarmer/minitouch-prebuilt
# extract prebuilt/<abi>/bin/{minicap,minitouch}
#         prebuilt/<abi>/lib/android-<sdk>/minicap.so
```
The agent falls back to `adb exec-out screencap` (mirror) and `adb shell input`
(touch) for any device whose ABI/SDK isn't vendored, so unknown devices still
work — just slower.

These binaries are third-party; see each project's LICENSE (Apache-2.0).

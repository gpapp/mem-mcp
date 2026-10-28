# Host-side operations

Scripts here run on the **docker host**, not in a container, and they are
deployed by hand like `nginx_snippet.conf`. Nothing in `mem-mcp/` can replace
them: the app containers have no docker socket and no `nvidia-smi`, and the
state they act on belongs to the host.

## Why a GPU watchdog exists

The failure it targets is silent by construction. On 2026-10-28 the ollama log
held, in order:

```
sched.go:1341  updated VRAM ... gpu=0 library=CUDA total="3.6 GiB"          <- GPU is visible
ggml_cuda_init: failed to initialize CUDA: unknown error                    <- and cannot be used
device_info:  CPU : Intel(R) Core(TM) i5-7600 ...                            <- no CUDA0 device
common_params_fit_impl: projected to use 2570 MiB of host memory ... no changes needed
```

The scheduler saw the card over NVML, `llama-server` failed `cuInit`, the model
loaded with **zero** GPU layers, and `/api/ps` reported `size_vram: 0`. Every
request was then answered from the CPU at a latency that looks plausible, so
`ollama ps` printing `100% CPU` was the only visible symptom — and the app was
correct to show it.

Two layers catch that, and neither replaces the other:

1. **In the app** (`MEM_GPU_EXPECTED=1`, the default): a resident model with no
   VRAM is reported as a *warning* in the status widget, naming `ollama ps` and
   the log strings to search for. This is the "somebody has the dashboard open"
   layer.
2. **On the host** (this directory): a timer probes whether a loaded model
   actually gets VRAM, and can restart ollama or escalate. This is the "nobody
   is looking at 3am" layer.

## Install

```bash
install -m 0755 ops/gpu-watchdog.sh /usr/local/bin/gpu-watchdog.sh
install -m 0644 ops/gpu-watchdog.service /etc/systemd/system/
install -m 0644 ops/gpu-watchdog.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gpu-watchdog.timer
systemctl list-timers gpu-watchdog.timer
```

Configuration lives in `/etc/default/gpu-watchdog` (see the variable list
below); the unit starts with `EnvironmentFile=-`, so the file is optional.

Run it once by hand before trusting the timer:

```bash
GPU_WATCHDOG_ACTION=alert gpu-watchdog.sh; echo "exit=$?"
journalctl -t gpu-watchdog -n 50
```

## What it actually checks

Not `nvidia-smi`. A card can answer `nvidia-smi` perfectly and still fail
`cuInit` inside a container, which is exactly the incident above. The script
asks the one question that has a single meaning:

| observation | conclusion |
|---|---|
| `/dev/nvidia0` absent inside `ollama` | the GPU compose overlay was dropped — deploy with `-f docker-compose.yml -f docker-compose.gpu.yml` |
| `/dev/nvidia0` present but `/dev/nvidia-uvm` absent | CUDA initialises through `nvidia-uvm`, so `cuInit` fails with the same `unknown error` as a wedged card. Warned, not failed: the load below is the real verdict, and this note explains it. `docker exec ollama ls -l /dev/nvidia*` is the manual form of both checks |
| a resident model prints `… GPU` | healthy |
| a resident model prints `… CPU` | **CPU fallback** — confirmed once more after `RECHECK_SECONDS`, because a model still being placed can briefly report 0 VRAM |
| a resident model prints `… CPU/GPU` | a split: working, but the card is too small for the model. Logged, never alerted |
| nothing resident | loads a probe model (`GPU_WATCHDOG_PROBE_MODEL`, else the first installed one), inspects it, then `ollama stop`s it again |
| `ollama ps` unparseable | exit 2 with the raw rows — a watchdog that reports green because it could not read the answer is worse than none |

The probe costs one cold model load and is only paid when **no** model is
resident. It is unloaded afterwards so the watchdog never competes for the 4 GB
card (asr-mcp holds 1.25 GB of it during live notes).

## Actions

`GPU_WATCHDOG_ACTION` defaults to `alert`, which writes to the journal
(`logger -t gpu-watchdog -p user.err`) and runs `GPU_WATCHDOG_ALERT_CMD` if set.
Nothing is changed without an explicit opt-in, because everything above `alert`
has a cost the operator should choose.

| action | what it does | recoverable? |
|---|---|---|
| `alert` (default) | journal + optional hook | — |
| `restart` | `docker restart ollama` | re-execs the server, so `cuInit` runs again; clears a stuck process or a lost context. **The recommended automatic setting.** |
| `reset` | `nvidia-smi --gpu-reset` | usually refused on GeForce (reset is a data-centre feature). Attempted so the journal records which remedy applies |
| `reboot` | `systemctl reboot` | the only thing that clears a driver-level failure (a wedged context or a userspace/kernel mismatch after a driver update) |

Escalation guards, both in `GPU_WATCHDOG_STATE_DIR`:

- **strikes** — consecutive failed probes. `reboot` waits for
  `GPU_WATCHDOG_STRIKES_BEFORE_REBOOT` (default 2), so one bad sample does not
  reboot the machine.
- **rate limit** — `restart`/`reset` at most once per
  `GPU_WATCHDOG_ACTION_MIN_INTERVAL_SECONDS` (1800), `reboot` at most once per
  `GPU_WATCHDOG_REBOOT_MIN_INTERVAL_SECONDS` (21600), so a machine that comes
  back broken does not get rebooted in a loop.

A successful probe clears both.

## A reset from inside the container is not possible, and would not help

Worth stating because it is the natural first idea: the ollama container has no
`nvidia-smi`, no docker socket, and no way to ask the host to reinitialise a
device. Even with those, `nvidia-smi --gpu-reset` is unsupported on a GeForce
GTX 1650 and requires exclusive access to the card, which asr-mcp does not
give up. And the failure in the incident is `cuInit` failing **before** any
allocation, i.e. a driver/context problem below the device — which a device
reset does not fix either. `docker restart ollama` (action `restart`) is the
cheap repair that does work when the driver is healthy.

## Configuration

| variable | default | meaning |
|---|---|---|
| `GPU_WATCHDOG_CONTAINER` | `ollama` | container to inspect |
| `GPU_WATCHDOG_ACTION` | `alert` | `alert` / `restart` / `reset` / `reboot` |
| `GPU_WATCHDOG_PROBE_MODEL` | first installed | model loaded when nothing is resident |
| `GPU_WATCHDOG_RECHECK_SECONDS` | `20` | second sample before declaring a fallback |
| `GPU_WATCHDOG_PROBE_TIMEOUT` | `180` | a load slower than this is itself a finding |
| `GPU_WATCHDOG_STATE_DIR` | `/var/lib/gpu-watchdog` | strike counter and rate-limit stamp |
| `GPU_WATCHDOG_STRIKES_BEFORE_REBOOT` | `2` | consecutive failures before `reboot` acts |
| `GPU_WATCHDOG_ACTION_MIN_INTERVAL_SECONDS` | `1800` | restart/reset floor |
| `GPU_WATCHDOG_REBOOT_MIN_INTERVAL_SECONDS` | `21600` | reboot floor |
| `GPU_WATCHDOG_ALERT_CMD` | *(empty)* | shell hook; reason also in `GPU_WATCHDOG_REASON` and stdin |

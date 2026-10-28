#!/usr/bin/env bash
# gpu-watchdog.sh -- detect (and optionally repair) a silent CPU fallback.
#
# Runs on the DOCKER HOST, not inside a container, and that is deliberate:
#   * the mem-mcp and ollama containers have no docker socket and no
#     nvidia-smi, so neither can restart the other or reset the device;
#   * the failure is a property of the driver and the card (a wedged context, a
#     userspace/kernel mismatch after a driver update), which is host state.
# Deploy with ops/gpu-watchdog.{service,timer}; see ops/README.md.
#
# What it detects is *not* "is nvidia-smi happy". A card can answer nvidia-smi
# perfectly and still fail cuInit inside the container, which is exactly the
# 2026-10-28 incident: the scheduler read 3.6 GiB of VRAM over NVML, then
# llama-server printed `ggml_cuda_init: failed to initialize CUDA: unknown
# error`, loaded zero GPU layers, and every request was served from the CPU at
# a plausible-looking latency. The only reliable signal is the one
# `ollama ps --format` prints and `/api/ps` reports as size_vram: a resident
# model with no VRAM. So this script asks the same question AGENTS.md tells an
# operator to ask, and nothing about the driver's opinion of itself.
#
# Exit codes: 0 healthy, 1 degraded (a failure was found and acted on),
# 2 configuration error, 3 Ollama could not be queried.

set -uo pipefail

OLLAMA_CONTAINER="${GPU_WATCHDOG_CONTAINER:-ollama}"
PROBE_MODEL="${GPU_WATCHDOG_PROBE_MODEL:-}"
RECHECK_SECONDS="${GPU_WATCHDOG_RECHECK_SECONDS:-20}"
PROBE_TIMEOUT="${GPU_WATCHDOG_PROBE_TIMEOUT:-180}"
ACTION="${GPU_WATCHDOG_ACTION:-alert}"
STATE_DIR="${GPU_WATCHDOG_STATE_DIR:-/var/lib/gpu-watchdog}"
STRIKES_BEFORE_REBOOT="${GPU_WATCHDOG_STRIKES_BEFORE_REBOOT:-2}"
ACTION_MIN_INTERVAL="${GPU_WATCHDOG_ACTION_MIN_INTERVAL_SECONDS:-1800}"
REBOOT_MIN_INTERVAL="${GPU_WATCHDOG_REBOOT_MIN_INTERVAL_SECONDS:-21600}"
ALERT_CMD="${GPU_WATCHDOG_ALERT_CMD:-}"

log()  { printf 'gpu-watchdog: %s\n' "$*"; }
warn() { printf 'gpu-watchdog: %s\n' "$*" >&2; }

# --- state -----------------------------------------------------------------
# Two counters, both in the state dir so a container rebuild cannot lose them.
# `strikes` is consecutive failed probes: a single bad sample (a model
# mid-load, a card that was busy) must not escalate to a reboot, so the reboot
# action requires the failure to be reproducible on the next run.
mkdir -p "$STATE_DIR" 2>/dev/null || true
strikes_file="$STATE_DIR/strikes"
last_action_file="$STATE_DIR/last-action"

read_count() { cat "$1" 2>/dev/null || echo 0; }
increment_strikes() { echo "$(( $(read_count "$strikes_file") + 1 ))" > "$strikes_file"; }
clear_strikes() { rm -f "$strikes_file"; }

# Rate limiting is a separate guard from the strike counter: strikes say "this
# is real", the interval says "do not restart or reboot in a loop while the
# machine comes back up and the timer fires again".
recently_acted() {
    local min_interval="$1" last now
    last="$(read_count "$last_action_file")"
    now="$(date +%s)"
    [[ "$last" =~ ^[0-9]+$ ]] || return 1
    (( now - last < min_interval ))
}

stamp_action() { date +%s > "$last_action_file"; }

# --- queries ---------------------------------------------------------------
# One line per resident model: "<name> <processor>". `ollama ps` prints
#   NAME  ID  SIZE  PROCESSOR  UNTIL
# and PROCESSOR is `100% GPU`, `100% CPU` or `49%/51% CPU/GPU` -- the split form
# is a single token, so the two columns are fields 4 and 5 and the UNTIL text
# (which contains spaces) is everything after them.
ps_rows() {
    docker exec "$OLLAMA_CONTAINER" ollama ps 2>/dev/null \
        | awk 'NR > 1 && NF >= 5 { print $1, $4, $5 }'
}

# --- health ----------------------------------------------------------------
# Classifies one processor cell. Returns 0 for GPU, 1 for CPU, 2 for a split
# (the documented consequence of a card too small for the model -- slow, but
# working, so it is logged and not alerted on).
classify() {
    case "$1" in
        *GPU/*CPU*|*CPU/*GPU*) return 2 ;;
        *GPU*) return 0 ;;
        *CPU*) return 1 ;;
    esac
    return 3
}

probe_model() {
    if [[ -z "$PROBE_MODEL" ]]; then
        PROBE_MODEL="$(docker exec "$OLLAMA_CONTAINER" ollama list 2>/dev/null \
            | awk 'NR > 1 { print $1; exit }')"
    fi
    if [[ -z "$PROBE_MODEL" ]]; then
        warn "no model is installed in $OLLAMA_CONTAINER, so there is nothing to probe with"
        exit 2
    fi
}

# Loads the probe model and leaves it resident for the caller to inspect. The
# prompt is one token: this is a load, not an inference benchmark, and the cost
# the operator pays is a cold model load on a card they already know is idle
# (nothing was resident) or broken.
load_probe() {
    probe_model
    log "loading probe model $PROBE_MODEL"
    timeout "$PROBE_TIMEOUT" docker exec "$OLLAMA_CONTAINER" \
        ollama run --keepalive=2m "$PROBE_MODEL" "ping" >/dev/null 2>&1
    # A timeout here is itself a finding: a model that will not load in three
    # minutes is the "card wedged" shape, and saying so beats timing out.
    local rc=$?
    if (( rc == 124 )); then
        warn "probe model $PROBE_MODEL did not finish loading within ${PROBE_TIMEOUT}s"
        return 1
    fi
    return "$rc"
}

unload_probe() {
    [[ -n "${PROBE_MODEL:-}" ]] || return 0
    docker exec "$OLLAMA_CONTAINER" ollama stop "$PROBE_MODEL" >/dev/null 2>&1 || true
}

# --- actions ---------------------------------------------------------------
alert() {
    local reason="$1"
    warn "$reason"
    logger -t gpu-watchdog -p user.err "$reason" 2>/dev/null || true
    if [[ -n "$ALERT_CMD" ]]; then
        # Operator-supplied hook (mail, webhook). The reason is passed in $1
        # and on stdin; the message is the only thing that has to survive.
        printf '%s\n' "$reason" | GPU_WATCHDOG_REASON="$reason" bash -c "$ALERT_CMD" || \
            warn "alert hook failed"
    fi
}

restart_ollama() {
    if recently_acted "$ACTION_MIN_INTERVAL"; then
        log "skipping restart: last restart/reset was less than ${ACTION_MIN_INTERVAL}s ago"
        return 0
    fi
    log "restarting $OLLAMA_CONTAINER (re-execs the server, so cuInit runs again)"
    stamp_action
    docker restart "$OLLAMA_CONTAINER" >/dev/null 2>&1 || warn "docker restart failed"
}

reset_gpu() {
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        warn "nvidia-smi is not on PATH, cannot reset the GPU"
        return 0
    fi
    if recently_acted "$ACTION_MIN_INTERVAL"; then
        log "skipping reset: last restart/reset was less than ${ACTION_MIN_INTERVAL}s ago"
        return 0
    fi
    # GeForce parts report "GPU Reset Failed" / "not supported" here -- reset is
    # a data-centre feature. It is attempted rather than assumed because the
    # answer differs by driver and by card, and a clear failure in the journal
    # is what tells the operator which of the two remedies applies.
    log "attempting nvidia-smi --gpu-reset"
    stamp_action
    nvidia-smi --gpu-reset >/dev/null 2>&1 \
        && log "GPU reset reported success" \
        || warn "GPU reset refused (expected on GeForce): the remedy is a host reboot"
}

reboot_host() {
    local strikes="$1"
    if (( strikes < STRIKES_BEFORE_REBOOT )); then
        log "failure strike $strikes/$STRIKES_BEFORE_REBOOT -- not rebooting yet"
        return 0
    fi
    if recently_acted "$REBOOT_MIN_INTERVAL"; then
        log "skipping reboot: last action was less than ${REBOOT_MIN_INTERVAL}s ago"
        return 0
    fi
    log "rebooting the host: a wedged GPU is not recoverable from user space"
    stamp_action
    systemctl reboot
}

remedy() {
    local reason="$1" strikes="$2"
    case "$ACTION" in
        alert)   : ;;
        restart) restart_ollama ;;
        reset)   reset_gpu ;;
        reboot)  reboot_host "$strikes" ;;
        *)       warn "unknown GPU_WATCHDOG_ACTION='$ACTION', treating it as 'alert'" ;;
    esac
    alert "$reason"
}

# --- main ------------------------------------------------------------------
if ! docker inspect -f '{{.State.Running}}' "$OLLAMA_CONTAINER" 2>/dev/null | grep -q true; then
    increment_strikes
    remedy "container $OLLAMA_CONTAINER is not running" "$(read_count "$strikes_file")"
    exit 3
fi

# The documented recurring failure, and a different remedy from a wedged card:
# the GPU compose overlay is a SEPARATE file, so a bare `docker compose up -d`
# recreates ollama with no device reservation and it looks identical from the
# API (models answer, on the CPU).
if ! docker exec "$OLLAMA_CONTAINER" test -e /dev/nvidia0 2>/dev/null; then
    increment_strikes
    remedy "no /dev/nvidia0 in $OLLAMA_CONTAINER -- the GPU overlay was dropped; deploy with -f docker-compose.yml -f docker-compose.gpu.yml" \
        "$(read_count "$strikes_file")"
    exit 1
fi

rows="$(ps_rows)"
if [[ -z "$rows" ]]; then
    # Nothing resident: the state of the card is unobservable, so observe it.
    if ! load_probe; then
        increment_strikes
        remedy "probe model $PROBE_MODEL failed to load -- the GPU may be wedged" \
            "$(read_count "$strikes_file")"
        exit 1
    fi
    rows="$(ps_rows)"
fi

healthy=0
broken=0
split=0
while read -r name processor detail; do
    [[ -n "$name" ]] || continue
    cell="$processor${detail:+ $detail}"
    classify "$cell"
    case $? in
        0) healthy=$((healthy + 1)) ;;
        1) broken=$((broken + 1)); warn "$name is resident with no GPU layers ($cell)" ;;
        2) split=$((split + 1)); log "$name is split across CPU/GPU ($cell) -- slow but working" ;;
        *) warn "$name: could not classify processor '${cell:-<empty>}'" ;;
    esac
done <<< "$rows"

if (( healthy == 0 && broken == 0 && split == 0 )); then
    # Every row was unparseable. A watchdog that reports green because it could
    # not read the answer is worse than no watchdog.
    increment_strikes
    remedy "could not parse 'ollama ps' output: $(printf '%s' "$rows" | head -3 | tr '\n' '|')" \
        "$(read_count "$strikes_file")"
    exit 2
fi

if (( broken > 0 )); then
    # A loaded model can lose the device moments after the load reports
    # success, so confirm before acting: the second sample is what separates a
    # real fallback from a model that was still being placed.
    sleep "$RECHECK_SECONDS"
    rows="$(ps_rows)"
    broken=0
    while read -r name processor detail; do
        [[ -n "$name" ]] || continue
        cell="$processor${detail:+ $detail}"
        classify "$cell"; [[ $? -eq 1 ]] && broken=$((broken + 1))
    done <<< "$rows"

    if (( broken > 0 )); then
        strikes="$(increment_strikes; read_count "$strikes_file")"
        unload_probe
        remedy "$broken model(s) resident with no GPU layers -- llama-server fell back to the CPU; see the ollama log for 'ggml_cuda_init' or 'device lost'" \
            "$strikes"
        exit 1
    fi
    log "the fallback was transient: the model is on the GPU on re-check"
fi

unload_probe
clear_strikes
log "ok: $healthy on GPU, $split split, 0 on CPU"
exit 0

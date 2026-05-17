#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# OpenFocus Codex hook shim. Best-effort local signal forwarding.

KIND="${1:-unknown}"
SOCK="${OPENFOCUS_HOOK_SOCK:-$HOME/.openfocus/hooks.sock}"

safe_instance_id() {
  v=$(printf '%s' "$1" | tr -c 'A-Za-z0-9_.-' '-' | sed 's/^[-._]*//; s/[-._]*$//')
  [ -n "$v" ] && printf '%s' "$v" || printf 'default'
}

REGISTERED_INSTANCE=$(safe_instance_id "$OPENFOCUS_REGISTERED_INSTANCE_ID")
UID_VAL=$(id -u 2>/dev/null || printf '0')
SPOOL="${OPENFOCUS_HOOK_SPOOL_DIR:-/tmp/openfocus-agent-hooks-$UID_VAL/$REGISTERED_INSTANCE}"
ORIGIN_INSTANCE=""
if [ -n "${OPENFOCUS_INSTANCE_ID:-}" ]; then
  ORIGIN_INSTANCE=$(safe_instance_id "$OPENFOCUS_INSTANCE_ID")
fi
if [ -n "${OPENFOCUS_REGISTERED_INSTANCE_ID:-}" ]; then
  if [ -z "$ORIGIN_INSTANCE" ]; then
    [ "$REGISTERED_INSTANCE" = "default" ] || exit 0
  elif [ "$ORIGIN_INSTANCE" != "$REGISTERED_INSTANCE" ]; then
    exit 0
  fi
fi

PAYLOAD=$(cat)
[ -n "$PAYLOAD" ] || PAYLOAD="null"
TS=$(date +%s)

json_escape() {
  printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'
}

log_hook_error() {
  log_path="${OPENFOCUS_HOOK_LOG:-/tmp/openfocus-agent-hooks.log}"
  ts_text=$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date)
  printf '%s agent=codex kind=%s sock=%s %s\n' \
    "$ts_text" "$(json_escape "$KIND")" "$(json_escape "$SOCK")" "$1" \
    >>"$log_path" 2>/dev/null || true
}

send_envelope() {
  if command -v nc >/dev/null 2>&1; then
    printf '%s' "$ENVELOPE" | nc -w "${OPENFOCUS_HOOK_NC_TIMEOUT:-2}" -U "$SOCK" >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 0 ] && return 0
    log_hook_error "transport=nc rc=$rc"
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$ENVELOPE" | OPENFOCUS_HOOK_SOCK="$SOCK" python3 -c '
import os
import socket
import sys

payload = sys.stdin.buffer.read()
sock_path = os.environ.get("OPENFOCUS_HOOK_SOCK") or ""
timeout = float(os.environ.get("OPENFOCUS_HOOK_NC_TIMEOUT") or "2")
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(timeout)
try:
    sock.connect(sock_path)
    sock.sendall(payload)
finally:
    sock.close()
' >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 0 ] && return 0
    log_hook_error "transport=python rc=$rc"
  fi
  return 1
}

write_spool() {
  mkdir -p "$SPOOL" >/dev/null 2>&1 || {
    log_hook_error "transport=spool rc=mkdir_failed spool=$(json_escape "$SPOOL")"
    return 1
  }
  base=$(printf '%s.%s.%s' "$(date +%s)" "$$" "${RANDOM:-0}")
  tmp="$SPOOL/.$base.tmp"
  final="$SPOOL/$base.json"
  printf '%s' "$ENVELOPE" >"$tmp" 2>/dev/null && mv "$tmp" "$final" 2>/dev/null && return 0
  rm -f "$tmp" >/dev/null 2>&1 || true
  log_hook_error "transport=spool rc=write_failed spool=$(json_escape "$SPOOL")"
  return 1
}

PPID_VAL=${PPID:-0}
TTY_RAW=$(tty 2>/dev/null)
case "$TTY_RAW" in
  /dev/tty*|/dev/ttys*) TTY="$TTY_RAW" ;;
  *)
    TTY_FROM_PPID=$(ps -o tty= -p "$PPID_VAL" 2>/dev/null | awk 'NF {print $1; exit}')
    case "$TTY_FROM_PPID" in
      /dev/tty*|/dev/ttys*) TTY="$TTY_FROM_PPID" ;;
      tty*|ttys*) TTY="/dev/$TTY_FROM_PPID" ;;
      *) TTY="" ;;
    esac
    ;;
esac

CWD=$(pwd 2>/dev/null)
RUNTIME=$(printf '{"cwd":"%s","tty":"%s","ppid":%s,"term_program":"%s","openfocus_instance_id":"%s","openfocus_hook_sock":"%s","openfocus_hook_spool_dir":"%s","openfocus_task_id":"%s","openfocus_session_id":"%s","openfocus_terminal_id":"%s"}' \
  "$(json_escape "$CWD")" \
  "$(json_escape "$TTY")" \
  "$PPID_VAL" \
  "$(json_escape "$TERM_PROGRAM")" \
  "$(json_escape "$OPENFOCUS_INSTANCE_ID")" \
  "$(json_escape "$SOCK")" \
  "$(json_escape "$SPOOL")" \
  "$(json_escape "$OPENFOCUS_TASK_ID")" \
  "$(json_escape "$OPENFOCUS_AGENT_SESSION_ID")" \
  "$(json_escape "$OPENFOCUS_TERMINAL_ID")")

ENVELOPE=$(printf '{"schema_version":1,"agent_runtime":"codex","hook_kind":"%s","runtime_ts":%s,"runtime":%s,"payload":%s}\n' \
  "$(json_escape "$KIND")" "$TS" "$RUNTIME" "$PAYLOAD")

send_envelope || write_spool || true
exit 0

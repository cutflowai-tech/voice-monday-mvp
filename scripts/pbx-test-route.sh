#!/usr/bin/env bash
# Enable (or remove) the voice-monday-agent test route on a plain Asterisk host.
#
#   sudo scripts/pbx-test-route.sh apply    # backup + additive changes + reload + verify
#   sudo scripts/pbx-test-route.sh revert   # undo exactly those changes + reload
#
# What "apply" changes (and nothing else):
#   http.conf       uncomment `enabled=yes` and `bindport=8088` (bindaddr must already be 127.0.0.1)
#   ari.conf        append the [voice-monday] user from build/asterisk/ari_voice_monday.conf
#   extensions.conf append `include => voice-monday-test` + `#include extensions_voice_monday.conf`
#                   (the last context in the file must be the phones' context, PHONE_CONTEXT)
#   new files       /etc/asterisk/extensions_voice_monday.conf, /var/lib/asterisk/sounds/voice-monday/
#                   and /var/spool/asterisk/recording/ when the distro did not create it
# Every appended block sits between `; >>> voice-monday-agent` and `; <<< voice-monday-agent`.
set -euo pipefail

ETC=/etc/asterisk
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SNIPPETS="$HERE/build/asterisk"
SOUNDS=/var/lib/asterisk/sounds/voice-monday
RECORDINGS=/var/spool/asterisk/recording
BEGIN="; >>> voice-monday-agent (added by scripts/pbx-test-route.sh; revert removes this block)"
END="; <<< voice-monday-agent"
EXT="$(grep -E '^ASTERISK_TEST_EXTENSION=' "$HERE/.env" 2>/dev/null | cut -d= -f2)"
EXT="${EXT:-7999}"
CONTEXT="${PHONE_CONTEXT:-office-internal}"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
ast() { asterisk -rx "$1"; }
# make sure a file ends with a newline before appending a marker block
end_nl() { [[ -z "$(tail -c1 "$1")" ]] || echo >>"$1"; }

apply() {
  for f in ari_voice_monday.conf extensions_voice_monday.conf; do
    [[ -f "$SNIPPETS/$f" ]] || { echo "missing $SNIPPETS/$f (run render-asterisk)" >&2; exit 1; }
  done
  grep -qE '^bindaddr=127\.0\.0\.1$' "$ETC/http.conf" \
    || { echo "http.conf bindaddr is not 127.0.0.1; refusing to enable HTTP" >&2; exit 1; }

  local backup
  backup="/etc/asterisk.bak-voice-monday-$(date +%Y%m%d-%H%M%S)"
  cp -a "$ETC" "$backup"
  echo "backup: $backup"

  sed -i -e 's/^;enabled=yes$/enabled=yes/' -e 's/^;bindport=8088$/bindport=8088/' "$ETC/http.conf"

  if ! grep -qF "$BEGIN" "$ETC/ari.conf"; then
    end_nl "$ETC/ari.conf"
    { printf '%s\n' "$BEGIN"; cat "$SNIPPETS/ari_voice_monday.conf"; printf '%s\n' "$END"; } >>"$ETC/ari.conf"
  fi

  install -m 0640 -o root -g asterisk "$SNIPPETS/extensions_voice_monday.conf" "$ETC/"
  if ! grep -qF "$BEGIN" "$ETC/extensions.conf"; then
    end_nl "$ETC/extensions.conf"
    printf '%s\ninclude => voice-monday-test\n#include extensions_voice_monday.conf\n%s\n' \
      "$BEGIN" "$END" >>"$ETC/extensions.conf"
  fi

  # numeric ids: the agent container's uid/gid has no account on the host
  # (uutils `install -o 10001` rejects that, chown accepts it)
  mkdir -p "$SOUNDS"
  chown 10001:10001 "$SOUNDS"
  chmod 0755 "$SOUNDS"

  # ARI /channels/{id}/record stores files below this directory. Some
  # distributions do not create it, causing ARI to return HTTP 500 with
  # "No such file or directory" on the first recording attempt.
  mkdir -p "$RECORDINGS"
  chown asterisk:asterisk "$RECORDINGS"
  chmod 0750 "$RECORDINGS"

  ast "module reload http"
  ast "module reload res_ari.so"
  ast "dialplan reload"
  verify
}

revert() {
  sed -i -e 's/^enabled=yes$/;enabled=yes/' -e 's/^bindport=8088$/;bindport=8088/' "$ETC/http.conf"
  for f in ari.conf extensions.conf; do
    sed -i "\\|^; >>> voice-monday-agent|,\\|^; <<< voice-monday-agent|d" "$ETC/$f"
  done
  rm -f "$ETC/extensions_voice_monday.conf"
  ast "dialplan reload"
  ast "module reload res_ari.so"
  ast "module reload http"
  verify
  echo "reverted (runtime folders $SOUNDS and $RECORDINGS were left in place)"
}

verify() {
  echo "--- http";      ast "http show status" | grep -E "Server|Bound" || true
  echo "--- ari users"; ast "ari show users"
  echo "--- dialplan";  ast "dialplan show $EXT@$CONTEXT" | grep -E "$EXT|Stasis|no existence" || true
}

case "${1:-}" in
  apply) apply ;;
  revert) revert ;;
  verify) verify ;;
  *) echo "usage: sudo $0 apply|revert|verify" >&2; exit 2 ;;
esac

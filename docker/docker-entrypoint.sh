#!/bin/sh
# Datenordner dem gewünschten Benutzer geben und Ebay Watcher ohne Root-Rechte starten.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
mkdir -p /data
if [ "$(id -u)" = "0" ]; then
  chown -R "$PUID:$PGID" /data
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"

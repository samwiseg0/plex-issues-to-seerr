FROM python:3.15-alpine3.24

# /data belongs to the runtime UID, so a new named volume starts out writable.
RUN mkdir /data && chown 10001:10001 /data
COPY plex_issues.py LICENSE /app/

ENV PYTHONUNBUFFERED=1 STATE_FILE=/data/state.json HEARTBEAT_FILE=/data/heartbeat
VOLUME /data
USER 10001:10001
WORKDIR /data

# Unhealthy when the heartbeat is missing or older than 3 x POLL_INTERVAL.
# The poller deletes the heartbeat at startup and rewrites it after each successful poll.
# The start period covers the first poll plus two failed polls at the default interval.
HEALTHCHECK --interval=1m --timeout=10s --start-period=15m --retries=3 \
    CMD ["python", "-c", "import os, sys, time; sys.exit(time.time() - os.path.getmtime(os.environ['HEARTBEAT_FILE']) > 3 * int(os.environ.get('POLL_INTERVAL') or 300))"]

# POLL_INTERVAL unset or empty, as in a bare POLL_INTERVAL= line in .env, means 300.
ENTRYPOINT ["/bin/sh", "-c", "export POLL_INTERVAL=\"${POLL_INTERVAL:-300}\"; exec python /app/plex_issues.py \"$@\"", "plex_issues"]

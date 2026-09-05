#!/bin/sh
set -eu

# Proxy trust is a deployment fact, not a build-time constant: SC_TRUSTED_PROXY_IPS
# (comma-separated IPs/CIDRs of the reverse proxy) tells uvicorn which peer
# it may trust X-Forwarded-For/X-Forwarded-Proto from. Without this, those
# headers are never trusted — request.client.host stays the direct TCP peer,
# which is the safe default (see backend/app/config.py's SC_ENV=production
# fail-fast check for the same setting).
extra_args=""
if [ -n "${SC_TRUSTED_PROXY_IPS:-}" ]; then
    extra_args="--proxy-headers --forwarded-allow-ips=${SC_TRUSTED_PROXY_IPS}"
fi

exec uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 $extra_args

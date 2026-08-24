# The agent, packaged for a server. On a laptop you never need this — run
# start.sh instead. This image exists for the always-on demo case: a small VPS
# with docker-compose.deploy.yml and a Cloudflare tunnel in front.
#
#   docker compose -f docker-compose.deploy.yml up -d
#
# Design notes, so nobody "fixes" them into bugs later:
#
# - Runs as root, deliberately. The container bind-mounts .env from the host,
#   and set_env_values() chmods it to 0600 on every write — which a non-root
#   user that does not own the host file cannot do. The container is
#   single-tenant and runs one process; root inside it owns nothing that
#   matters beyond its own mounts.
# - Binds 0.0.0.0, deliberately. Inside the container that is the only way the
#   tunnel sidecar can reach it; compose publishes the port to the host's
#   loopback only, so nothing is reachable from the internet except through
#   the tunnel — which is the point of using one.

FROM python:3.12-slim

WORKDIR /app

# Layer-cached: requirements change far less often than source. The lock file
# constrains every transitive dependency to the exact versions the test suite
# ran against, so a rebuild months from now is still the build that was tested.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock

COPY src/ src/
COPY LICENSE .

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

# State the container writes; compose mounts volumes over these.
RUN mkdir -p /app/data /app/out /app/credentials /app/prompts && touch /app/.env

EXPOSE 8765

# GET / serves the login page without auth, so it doubles as the health probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=4)"]

CMD ["python", "-m", "companies_research", "ui", "--host", "0.0.0.0", "--no-browser"]

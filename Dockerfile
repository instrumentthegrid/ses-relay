# Digest-pinned Chainguard Python: publicly pullable without auth, nonroot by
# default, and the runtime stage has no shell or package manager. Dependabot
# bumps both digests weekly.
FROM cgr.dev/chainguard/python:latest-dev@sha256:7e3a3c3c8231458251910f5631d84f9d749d370e7317f5b84c5208fbab8bbef5 AS builder

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --prefix=/home/nonroot/install -r requirements.txt

FROM cgr.dev/chainguard/python:latest@sha256:a1775c7276078865461ee5714954284f12809f333433d856d720b249c65c11b2

COPY --from=builder /home/nonroot/install /usr/
COPY ses_relay.py /app/ses_relay.py

EXPOSE 2525
# No shell in this image, so the check is plain Python: is the port answering?
HEALTHCHECK --interval=30s --timeout=5s \
  CMD ["python", "-c", "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('LISTEN_PORT', '2525'))), 3)"]

# The base image's entrypoint is python.
CMD ["/app/ses_relay.py"]

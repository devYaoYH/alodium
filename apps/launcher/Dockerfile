# On-demand app lifecycle controller.
# Needs docker CLI + compose plugin for compose operations via the socket.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y docker.io docker-compose-v2 && rm -rf /var/lib/apt/lists/*

COPY app.py /srv/launcher.py

EXPOSE 8081

# Ensure the audit log directory exists and is writable by nobody
# so the launcher_audit volume mount (created at runtime) has correct ownership.
RUN mkdir -p /var/log/launcher && chown nobody:nogroup /var/log/launcher

USER nobody

CMD ["python3", "/srv/launcher.py"]
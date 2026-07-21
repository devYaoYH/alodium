# On-demand app lifecycle controller.
# Needs docker CLI + compose plugin for compose operations via the socket.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y ca-certificates curl && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && apt-get install -y docker.io docker-compose-plugin && rm -rf /var/lib/apt/lists/*

COPY app.py /srv/launcher.py

EXPOSE 8081

# Ensure the audit log directory exists and is writable by nobody
# so the launcher_audit volume mount (created at runtime) has correct ownership.
RUN mkdir -p /var/log/launcher && chown nobody:nogroup /var/log/launcher

USER nobody

CMD ["python3", "/srv/launcher.py"]
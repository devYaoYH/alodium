# egress-broker — single image, three roles via EGRESS_ROLE. Mirrors the
# search-broker image (python:3.12-slim, non-root, stdlib + psycopg only).
# Built locally from this directory (floor precedent: build: . in compose,
# :local tag); the manifest carries no [build] repo because the source lives
# in node-config itself.
FROM python:3.12-slim

RUN useradd -m -u 10001 app
USER app
WORKDIR /app
COPY --chown=app:app requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app . .

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
  CMD python3 -c "import os, urllib.request as u; u.urlopen('http://localhost:'+os.environ.get('PORT', '8080')+'/healthz', timeout=2)"

CMD ["python3", "app.py"]

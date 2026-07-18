# Skeleton service image. Re-pin to a digest before the first real deploy
# (scripts/pin-images.sh handles compose images; app images pin here).
FROM python:3.12-slim

RUN useradd -m -u 10001 app
USER app
WORKDIR /app
COPY --chown=app:app . .

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
  CMD python3 -c "import urllib.request as u; u.urlopen('http://localhost:8080/healthz', timeout=2)"

CMD ["python3", "app.py"]

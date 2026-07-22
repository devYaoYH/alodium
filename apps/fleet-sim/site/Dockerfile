# Simple static server for the Fleet Simulator game
FROM python:3.12-slim

# Single-file site, no external assets
COPY index.html /site/index.html

# Non-root runtime (Debian slim already has a nobody user)
USER nobody

EXPOSE 8080

CMD ["python3", "-m", "http.server", "8080", "--directory", "/site"]
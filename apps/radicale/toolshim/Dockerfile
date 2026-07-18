# radicale-toolshim — stdlib-only tool server; nothing to pip install, nothing
# to mirror beyond the base image (same base as templates/app-skeleton).
FROM python:3.12-slim
WORKDIR /app
COPY app.py .
USER nobody
EXPOSE 8100
CMD ["python3", "-u", "app.py"]

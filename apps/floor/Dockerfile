# Same pinned python base the registry runs on — stdlib only, no pip layer.
FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
WORKDIR /srv
COPY app.py /srv/app.py
COPY site /srv/site
EXPOSE 8080
USER nobody
CMD ["python", "/srv/app.py"]

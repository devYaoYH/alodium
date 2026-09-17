# Same pinned python base the registry runs on — stdlib only, no pip layer.
FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
WORKDIR /srv
COPY app.py /srv/app.py
COPY site /srv/site
# site/vendor is staged by scripts/fetch-vendor.sh on the HOST, from the pins
# in manifest/floor.toml — deliberately not fetched here, so this image stays
# an offline build with no registry credential in any layer. Fail loudly:
# a floor whose log panel silently has no terminal is worse than one that
# does not build.
RUN test -s /srv/site/vendor/xterm.js && test -s /srv/site/vendor/addon-fit.js \
 || (echo "site/vendor is empty — run ./scripts/fetch-vendor.sh floor first" >&2; exit 1)
EXPOSE 8080
USER nobody
CMD ["python", "/srv/app.py"]

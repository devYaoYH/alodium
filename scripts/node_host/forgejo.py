"""
forgejo — the host jobs' API client: the bash `A()` wrapper, without curl.

    A() { /usr/bin/curl -sk --resolve "git.${NODE_DOMAIN}:443:127.0.0.1" \\
          -H "Authorization: token $AGENT_FORGEJO_TOKEN" \\
          -H "Content-Type: application/json" "$@"; }

Three properties of that line are behavior, and are kept:

  --resolve …:127.0.0.1  The request goes to THIS host's Caddy, whatever DNS
                         says about git.<domain>. The pinned transport below
                         connects to 127.0.0.1:443 and sends git.<domain> as
                         SNI and Host, so Caddy picks the same site and cert.
  -k                     No certificate verification (the local-dev CA is not
                         in the host trust store; the peer is localhost).
  -s, no --fail          An HTTP error status still yields its body, and an
                         unreachable server yields NOTHING — no exception.
                         `get_text` returns "" then, and each caller decides
                         what an empty body means, exactly where the bash's
                         `| python3 -c 'json.load(...)'` decided it. Writes
                         return curl's exit status (0 for any HTTP response),
                         because under `set -e` that status is what aborted a
                         pass. The #62 hardening (preflight, --fail) belongs
                         in a follow-up on top of this, not inside a port.

`transport` is injectable: tests pass a fake that answers from a table and
records every request, so nothing in the test suite opens a socket.
"""

import http.client
import json
import socket
import ssl
from urllib.parse import urlsplit

# curl's exit codes for the failures a pinned localhost request can hit.
CURL_COULDNT_CONNECT = 7
CURL_TIMEOUT = 28
CURL_SSL = 35

# curl's default connect timeout; the bash set no total timeout on git calls.
DEFAULT_TIMEOUT = 300


class Unreachable(OSError):
    """The request never got an HTTP response. `.code` is curl's exit status."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to `pin_ip`, presenting `host` as SNI and Host — `--resolve`."""

    def __init__(self, host, pin_ip, port, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pin_ip = pin_ip

    def connect(self):
        sock = socket.create_connection((self._pin_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _insecure_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def pinned_transport(method, url, headers, body, timeout, pin_ip="127.0.0.1"):
    """(status, body bytes) for one request; raises Unreachable if there was
    no HTTP response at all."""
    parts = urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    conn = _PinnedHTTPSConnection(parts.hostname, pin_ip, parts.port or 443,
                                  timeout, _insecure_context())
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    except ssl.SSLError as exc:
        raise Unreachable(CURL_SSL, str(exc)) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise Unreachable(CURL_TIMEOUT, str(exc)) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise Unreachable(CURL_COULDNT_CONNECT, str(exc)) from exc
    finally:
        conn.close()


def fetch(transport, url, headers, timeout=DEFAULT_TIMEOUT) -> tuple:
    """`curl -s URL`: (body whatever the status, whether ANY response came).
    Unreachable is ("", False) — curl printed nothing and exited non-zero."""
    try:
        _, data = transport("GET", url, headers, None, timeout)
    except Unreachable:
        return "", False
    return data.decode("utf-8", errors="replace"), True


def get_text(transport, url, headers, timeout=DEFAULT_TIMEOUT) -> str:
    """`$(curl -s URL)` where nobody looked at curl's status."""
    return fetch(transport, url, headers, timeout)[0]


class Forgejo:
    """The coordination repo's API, as the host jobs' `A` + `$GAPI` saw it."""

    def __init__(self, domain: str, token: str, repo: str,
                 transport=pinned_transport):
        self.origin = f"https://git.{domain}"
        self.base = f"{self.origin}/api/v1/repos/{repo}"
        self._transport = transport
        self._headers = {"Authorization": f"token {token}",
                         "Content-Type": "application/json"}

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def get_text(self, path: str) -> str:
        return get_text(self._transport, self.url(path), self._headers)

    def send(self, method: str, path: str, payload=None) -> int:
        """A write. Returns curl's exit status: 0 for ANY HTTP response (the
        bash discarded the body with >/dev/null and never looked at the
        status), non-zero only if no response came back."""
        body = None if payload is None else json.dumps(payload).encode()
        try:
            self._transport(method, self.url(path), self._headers, body,
                            DEFAULT_TIMEOUT)
        except Unreachable as exc:
            return exc.code
        return 0

    # --- the calls the jobs make, named -----------------------------------

    def comment(self, num, body: str) -> int:
        return self.send("POST", f"issues/{num}/comments", {"body": body})

    def close(self, num) -> int:
        return self.send("PATCH", f"issues/{num}", {"state": "closed"})

    def add_label(self, num, label_id) -> int:
        return self.send("POST", f"issues/{num}/labels", {"labels": [int(label_id)]})

    def remove_label(self, num, label_id) -> int:
        return self.send("DELETE", f"issues/{num}/labels/{label_id}")


def label_id(labels_text: str, name: str) -> str:
    """The id of the label called `name`, as a string, "" if absent.

        python3 -c 'ids=[l["id"] for l in json.load(sys.stdin) if l["name"]==N];
                    print(ids[0] if ids else "")'

    Raises on a body that is not a JSON list of labels — which is what the
    bash's python did, and where that happened under `set -e` it ended the
    pass. Callers that ran it without `-e` catch and use "".
    """
    ids = [lab["id"] for lab in json.loads(labels_text) if lab["name"] == name]
    return str(ids[0]) if ids else ""

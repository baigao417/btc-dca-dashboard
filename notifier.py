"""Optional generic JSON webhook; credentials never enter the database."""
import json
import os
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


urlopen = build_opener(NoRedirect()).open


def notify(decision: dict) -> str:
    url = os.environ.get("BTC_WEBHOOK_URL", "")
    if not url:
        return "disabled"
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return "invalid_configuration"
    headers = {"Content-Type": "application/json"}
    credential = os.environ.get("BTC_WEBHOOK_TOKEN", "")
    if credential:
        headers["Authorization"] = "Bearer " + credential
    payload = {key: decision[key] for key in ("id", "action", "multiplier", "status")}
    try:
        request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urlopen(request, timeout=10) as response:
            return "sent" if 200 <= response.status < 300 else "failed"
    except Exception:
        # Exceptions can include credential-bearing URLs. Do not persist them.
        return "failed"

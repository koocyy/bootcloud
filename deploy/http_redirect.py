"""Tiny ASGI app for port 80: redirects every plain-HTTP request to HTTPS.

Run by bootcloud-redirect.service. The target host is fixed (not taken from
the request's Host header) so the redirect can't be pointed somewhere else.
"""

TARGET = b"https://bootcloud.eu"


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    location = TARGET + (scope.get("raw_path") or scope["path"].encode())
    if scope.get("query_string"):
        location += b"?" + scope["query_string"]
    await send({
        "type": "http.response.start",
        "status": 301,
        "headers": [(b"location", location), (b"content-length", b"0")],
    })
    await send({"type": "http.response.body", "body": b""})

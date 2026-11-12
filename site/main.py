import base64
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from jwcrypto import jwk, jwe


key = secrets.token_bytes(32)

jwe_key = jwk.JWK(
    kty="oct",
    k=base64.urlsafe_b64encode(key).rstrip(b"=").decode(),
)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/assets", StaticFiles(directory="assets"), name="assets")


def gen_jwt(ip: str) -> str:
    payload = {
        "ip": ip,
        "UID": secrets.token_urlsafe(10),
        "exp": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
    }

    token = jwe.JWE(
        json.dumps(payload).encode(),
        protected={
            "alg": "dir",
            "enc": "A256GCM",
        },
    )
    token.add_recipient(jwe_key)

    return token.serialize()


@app.get("/")
async def serve_main(request: Request):
    ip = request.client.host if request.client else ""

    response = FileResponse("index.html")
    response.set_cookie(
        key="session",
        value=gen_jwt(ip),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=86400,
    )

    return response
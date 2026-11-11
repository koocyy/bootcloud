from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

# The auto-generated API docs (/docs, /redoc, /openapi.json) are switched off
# so they are not public. For local development, remove the three arguments.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount('/assets', StaticFiles(directory='assets'), name='assets')


@app.get('/')
async def serve_main():
    return FileResponse('index.html')
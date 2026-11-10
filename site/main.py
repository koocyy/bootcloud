from fastapi import FastAPI
from starlette.responses import FileResponse 

app = FastAPI()


@app.get('/')
async def serve_main():
    return FileResponse('index.html')

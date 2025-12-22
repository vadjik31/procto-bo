import os
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/skillspace-webhook")
async def skillspace_webhook(request: Request):
    token = request.query_params.get("token")
    if token != os.getenv("WEBHOOK_SECRET"):
        raise HTTPException(status_code=403, detail="Invalid token")

    payload = await request.json()
    print("SKILLSPACE EVENT:", payload.get("name"))
    return {"ok": True}

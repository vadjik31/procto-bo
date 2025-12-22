import os
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

PASS_THRESHOLD = int(os.getenv("PASS_THRESHOLD", 50))
GREAT_THRESHOLD = int(os.getenv("GREAT_THRESHOLD", 80))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")


@app.get("/")
def root():
    return {"status": "ok"}


@app.post("/skillspace-webhook")
async def skillspace_webhook(request: Request):
    token = request.query_params.get("token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    payload = await request.json()
    event_name = payload.get("name")

    print("EVENT RECEIVED:", event_name)

    # Нас интересует окончание теста
    if event_name == "test-end":
        lesson = payload.get("lesson", {})
        score = lesson.get("score")

        if score is None:
            print("NO SCORE FOUND")
            return {"ok": True}

        if score < PASS_THRESHOLD:
            result = "FAILED"
        elif PASS_THRESHOLD <= score < GREAT_THRESHOLD:
            result = "PASSED"
        else:
            result = "GREAT"

        print(
            f"TEST RESULT | score={score} | result={result}"
        )

    return {"ok": True}

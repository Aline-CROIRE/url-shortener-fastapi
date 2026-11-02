from fastapi import FastAPI
app= FastAPI()

@app.get("/health")
def check_health():
    return {"status": "healthy"}
from fastapi import FastAPI

from app.database import lifespan
from app.indicators import router as indicators_router

app = FastAPI(title="hivtools-mcp", lifespan=lifespan)
app.include_router(indicators_router)


@app.get("/")
async def root():
    return {"message": "Hello World"}

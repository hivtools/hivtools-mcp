from fastapi import FastAPI
from fastmcp.utilities.lifespan import combine_lifespans
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.database import lifespan
from app.health import router as health_router
from app.indicators import router as indicators_router
from app.mcp import build_mcp_app
from app.ratelimit import limiter
from app.settings import settings
from app.version import get_name, get_version

app = FastAPI(
    title="hivtools-api",
    version=get_version(),
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
)
app.state.limiter = limiter
# slowapi's handler is typed with a narrower `exc: RateLimitExceeded` than Starlette's
# ExceptionHandler protocol; the pairing is correct and is slowapi's documented usage.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # ty: ignore[invalid-argument-type]
app.include_router(indicators_router)
app.include_router(health_router)


@app.get("/", tags=["meta"])
async def root():
    return {"name": get_name(), "version": get_version()}


@app.get("/version", tags=["meta"])
async def version():
    return {"name": get_name(), "version": get_version()}


mcp_app = build_mcp_app(app)

app.router.lifespan_context = combine_lifespans(lifespan, mcp_app.lifespan)
app.router.routes.extend(mcp_app.routes)

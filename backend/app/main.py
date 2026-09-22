import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import background_seeder
from .api.routes import router as api_router
from .config import FRONTEND_DIR
from .db import init_db

app = FastAPI(title="Radio Map")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()
    # Slowly precomputes coverage for every imported station in the
    # background so the map feels instant once it's had time to run.
    # Set RADIO_MAP_DISABLE_SEEDER=1 to turn it off (e.g. if you'd rather
    # control precompute timing yourself via the CLI).
    if not os.environ.get("RADIO_MAP_DISABLE_SEEDER"):
        background_seeder.start()


app.include_router(api_router)

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

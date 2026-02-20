# api.py
from fastapi import FastAPI

from endpoints.intent import router as intent_router
from endpoints.sql import router as sql_router
from endpoints.pipeline import router as pipeline_router
from endpoints.report import router as report_router
from endpoints.nl_to_pdf import router as nl_to_pdf_router

app = FastAPI(
    title="NL→SQL API",
    version="0.1.0",
    description="FastAPI wrapper around the NL→SQL pipeline (Agent-0, RAG, Agents 1-3, Oracle, reports).",
)

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins (good for dev/testing; for production, list specific origins like ["http://your-frontend-domain.com"])
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods (GET, POST, etc.)
    allow_headers=["*"],  # Allows all headers
)


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "message": "NL→SQL API is running",
    }


app.include_router(intent_router, prefix="/v1")
app.include_router(sql_router, prefix="/v1")
app.include_router(pipeline_router, prefix="/v1")
app.include_router(report_router, prefix="/v1")
app.include_router(nl_to_pdf_router, prefix="/v1")

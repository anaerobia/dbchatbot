"""FastAPI backend for the natural-language Oracle chatbot.

Flow for POST /api/ask:
  question -> Anthropic (generate SQL) -> read-only execute -> Anthropic
  (summarize) -> natural-language answer (+ the SQL and rows, for transparency).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()  # load backend/.env before anything reads os.environ

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import db  # noqa: E402
import llm  # noqa: E402

app = FastAPI(title="DB Chatbot")

_origins = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class HistoryTurn(BaseModel):
    """A prior turn: the user's question and the SQL that answered it."""

    question: str
    sql: str


class AskRequest(BaseModel):
    """A natural-language question, plus optional prior conversation turns."""

    question: str
    # Oldest first. The frontend sends the recent turns so follow-up questions
    # can resolve references (e.g. an omitted table name) from context.
    history: list[HistoryTurn] = []


class Chart(BaseModel):
    """A normalized chart the frontend can render directly."""

    type: str  # "pie" | "bar" | "line"
    title: str
    data: list[dict]  # [{"label": str, "value": float}, ...]


class AskResponse(BaseModel):
    """The answer plus the SQL and results used to produce it."""

    answer: str
    sql: str
    explanation: str
    columns: list[str]
    rows: list[list]
    row_count: int
    chart: Chart | None = None


def _build_chart(spec: dict, columns: list[str], rows: list[list]) -> Chart | None:
    """Turn the LLM chart spec + result rows into a renderable Chart, or None.

    Maps the spec's label/value column names (case-insensitively) to row
    indices and coerces values to float. Returns None if the spec asks for no
    chart, the columns don't match, or no numeric data results.
    """
    if not spec or spec.get("type", "none") == "none":
        return None

    upper = [c.upper() for c in columns]
    try:
        li = upper.index(spec["label_column"].upper())
        vi = upper.index(spec["value_column"].upper())
    except (ValueError, KeyError, AttributeError):
        return None

    data: list[dict] = []
    for row in rows:
        try:
            value = float(row[vi])
        except (TypeError, ValueError):
            continue  # skip non-numeric values
        data.append({"label": str(row[li]), "value": value})

    if not data:
        return None
    return Chart(type=spec["type"], title=spec.get("title", ""), data=data)


@app.get("/api/health")
def health() -> dict:
    """Cheap liveness check that also confirms the DB connection works."""
    try:
        db.run_select("SELECT 1 FROM dual", max_rows=1)
    except Exception as exc:  # noqa: BLE001 - surface any connectivity issue
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")
    return {"status": "ok"}


@app.get("/api/schema")
def schema() -> dict:
    """Return the schema description the LLM is given (useful for debugging)."""
    try:
        return {"schema": db.get_schema_description()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    """Answer a natural-language question about the database."""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty.")

    max_rows = int(os.environ.get("MAX_ROWS", "1000"))

    # 1. Natural language -> SQL
    try:
        schema_description = db.get_schema_description()
        history = [t.model_dump() for t in req.history]
        generated = llm.generate_sql(question, schema_description, history=history)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to generate SQL: {exc}")

    sql = generated["sql"]
    explanation = generated.get("explanation", "")

    # 2. Execute (read-only guard inside db.run_select)
    try:
        columns, rows = db.run_select(sql, max_rows=max_rows)
    except db.UnsafeQueryError as exc:
        raise HTTPException(status_code=400, detail=f"Rejected unsafe query: {exc}")
    except Exception as exc:  # noqa: BLE001 - Oracle errors, etc.
        raise HTTPException(
            status_code=400,
            detail=f"Query failed: {exc}\n\nGenerated SQL:\n{sql}",
        )

    # Build the chart first so the summary knows a graphic is being shown.
    chart = _build_chart(generated.get("chart"), columns, rows)

    # 3. Results -> natural language
    try:
        answer = llm.summarize_answer(
            question, sql, columns, rows, has_chart=chart is not None
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to summarize answer: {exc}")

    return AskResponse(
        answer=answer,
        sql=sql,
        explanation=explanation,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        chart=chart,
    )

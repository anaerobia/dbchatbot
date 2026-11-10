"""LLM interaction (Anthropic API, or a local OpenAI-compatible server).

Two steps:

1. :func:`generate_sql` — turn a natural-language question into SQL for the
   selected database's dialect, using structured outputs so we get clean SQL
   back with no prose to strip. Questions that ask to *change* the database
   produce a ``statement_type: "write"`` result, which the app shows to the
   user to copy but never executes.
2. :func:`summarize_answer` — turn the query + result rows into a
   natural-language answer.

Provider is chosen with ``LLM_PROVIDER``:

- ``anthropic`` (default): the official ``anthropic`` SDK (Messages API),
  model from ``ANTHROPIC_MODEL`` (default ``claude-opus-4-8``). Uses prompt
  caching and adaptive thinking.
- ``openai`` (aliases ``lmstudio``, ``local``): any OpenAI-compatible Chat
  Completions server -- LM Studio, Ollama, vLLM, llama.cpp server, ... --
  at ``LLM_BASE_URL`` (default LM Studio's ``http://localhost:1234/v1``) with
  model ``LLM_MODEL``. No API cost; quality depends on the local model.
"""

from __future__ import annotations

import json
import os

import re
from functools import cache

_OPENAI_ALIASES = {"openai", "lmstudio", "local"}


def _provider() -> str:
    """``"anthropic"`` or ``"openai"`` (any OpenAI-compatible server)."""
    name = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    if name in _OPENAI_ALIASES:
        return "openai"
    if name != "anthropic":
        raise RuntimeError(
            f"Unknown LLM_PROVIDER={name!r}; use 'anthropic' or 'lmstudio'/'openai'."
        )
    return name


@cache
def _anthropic_client():
    # Imported lazily so a local-only setup never needs ANTHROPIC_API_KEY.
    # The client reads ANTHROPIC_API_KEY (or an `ant auth login` profile).
    import anthropic

    return anthropic.Anthropic()


@cache
def _openai_client():
    from openai import OpenAI

    return OpenAI(
        base_url=os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1"),
        # Local servers ignore the key, but the SDK requires a non-empty one.
        api_key=os.environ.get("LLM_API_KEY", "lm-studio"),
        timeout=float(os.environ.get("LLM_TIMEOUT", "300")),
    )


def _model() -> str:
    if _provider() == "openai":
        model = os.environ.get("LLM_MODEL", "").strip()
        if not model:
            raise RuntimeError(
                "LLM_MODEL is not set. Set it to the model identifier shown in "
                "LM Studio (e.g. qwen2.5-coder-14b-instruct)."
            )
        return model
    return os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")


def _strip_reasoning(text: str) -> str:
    """Drop ``<think>...</think>`` blocks emitted by local reasoning models."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _parse_json(text: str) -> dict:
    """Parse a JSON object, tolerating code fences or prose around it.

    Claude's structured outputs return clean JSON; small local models
    sometimes wrap it in ```json fences or add a sentence before it.
    """
    text = _strip_reasoning(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise RuntimeError(f"Model did not return JSON: {text[:200]!r}")


def _openai_chat(
    system: str,
    messages: list[dict],
    max_tokens: int,
    json_schema: dict | None = None,
) -> str:
    """One Chat Completions call to the OpenAI-compatible server."""
    kwargs: dict = {}
    if json_schema is not None:
        # LM Studio enforces this schema with grammar-constrained sampling.
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "strict": True, "schema": json_schema},
        }
    response = _openai_client().chat.completions.create(
        model=_model(),
        max_tokens=max_tokens,
        temperature=0,
        messages=[{"role": "system", "content": system}, *messages],
        **kwargs,
    )
    u = response.usage
    if u is not None:
        print(
            f"[{_provider()}] input={u.prompt_tokens} output={u.completion_tokens}",
            flush=True,
        )
    return response.choices[0].message.content or ""


_SQL_SCHEMA = {
    "type": "object",
    "properties": {
        "statement_type": {
            "type": "string",
            "enum": ["read", "write"],
            "description": (
                "'read' for a single SELECT/WITH query that answers a question. "
                "'write' if the user asked to change data or structure "
                "(INSERT/UPDATE/DELETE/MERGE/DDL/grants/...)."
            ),
        },
        "sql": {
            "type": "string",
            "description": (
                "For 'read': one SELECT/WITH query, no trailing semicolon. For "
                "'write': the complete, ready-to-run statement(s), each ending "
                "with a semicolon."
            ),
        },
        "explanation": {
            "type": "string",
            "description": (
                "One short sentence explaining what the query does. For "
                "'write', say what it will change and note any risks."
            ),
        },
        "chart": {
            "type": "object",
            "description": "How to visualize the result, or type 'none' if a chart is not appropriate.",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["none", "pie", "bar", "line"],
                    "description": "Chart type. 'none' unless the user asked to plot/chart/graph or the data is an obvious category->number breakdown.",
                },
                "title": {"type": "string", "description": "Short chart title."},
                "label_column": {
                    "type": "string",
                    "description": "Result column name (exactly as it appears in the SELECT output) used for category labels / x-axis. Empty if type is none.",
                },
                "value_column": {
                    "type": "string",
                    "description": "Result column name holding the numeric value / y-axis. Empty if type is none.",
                },
            },
            "required": ["type", "title", "label_column", "value_column"],
            "additionalProperties": False,
        },
    },
    "required": ["statement_type", "sql", "explanation", "chart"],
    "additionalProperties": False,
}


# Static instructions — kept byte-identical across requests so they form part
# of the stable cached prefix (any change here invalidates the cache).
_SQL_INSTRUCTIONS = (
    "You are an expert SQL analyst. Given a database's SQL dialect, its "
    "schema, and a user's request, produce SQL for that database.\n\n"
    "Rules:\n"
    "- Write SQL in the dialect named in the database section below and "
    "follow its dialect rules.\n"
    "- If the user asks a question about the data, set statement_type to "
    "'read' and write a single SELECT (or WITH ... SELECT) query.\n"
    "- If the user asks to change the database (insert/update/delete rows, "
    "create/alter/drop objects, grant privileges, etc.), set statement_type "
    "to 'write' and write the complete statement(s) the user can copy and "
    "run themselves, each terminated with a semicolon. The app will NOT "
    "execute it. Never mix read and write statements in one answer, and never "
    "hide a write inside a SELECT.\n"
    "- Use only tables and columns that appear in the schema (a write may "
    "create new objects the user asked for).\n"
    "- Prefer COUNT/aggregate queries when the user asks 'how many'.\n"
    "- Match identifier spelling and case to the schema.\n"
    "- The conversation may include prior turns. If the new question omits "
    "the table (e.g. 'how many distinct collections?', 'and its columns?'), "
    "infer it from the most recently referenced table in the conversation. "
    "Only fall back to a best guess if inference is genuinely impossible; "
    "otherwise carry the context forward.\n"
    "- Charts: if the user asks to plot/chart/graph (or the answer is a natural "
    "category->number breakdown), set chart.type to the requested type "
    "('pie', 'bar', or 'line') and write SQL returning EXACTLY two columns: a "
    "category/label column and a numeric value column (use GROUP BY with "
    "COUNT/SUM). Set label_column and value_column to those output column names "
    "(exactly as the database will report them). Otherwise, and always for "
    "'write', set chart.type to 'none' and leave label_column/value_column "
    "empty."
)


def generate_sql(
    question: str,
    schema_description: str,
    dialect_context: str,
    history: list[dict] | None = None,
    failed_attempt: dict | None = None,
) -> dict:
    """Generate SQL for ``question`` in the target database's dialect.

    ``dialect_context`` names the dialect and server version and lists the
    dialect's rules (see :meth:`db.base.Database.dialect_context`).

    ``failed_attempt`` (``{"sql": str, "error": str}``) is a previous answer
    to this same question that the database rejected; the model is shown the
    error and asked for a corrected query.

    Returns a dict with ``statement_type`` (``"read"``/``"write"``), ``sql``,
    ``explanation`` and ``chart`` keys.

    ``history`` is an optional list of prior turns, each ``{"question": str,
    "sql": str}``, oldest first. It is replayed as alternating user/assistant
    messages so the model can resolve references (an omitted table name, "that
    table", "and its columns") against the most recently used table.

    The system prompt is split into two blocks: the static instructions and the
    (large, per-database but stable) dialect + schema block. A
    ``cache_control`` breakpoint on the schema block caches the whole system
    prefix, so repeated questions only pay full price
    for the volatile conversation. The schema is well above the 4096-token
    minimum cacheable prefix for Opus-tier models.
    """
    system = [
        {"type": "text", "text": _SQL_INSTRUCTIONS},
        {
            "type": "text",
            "text": (
                f"{dialect_context}\n\n"
                f"Schema (table(column type, ...)):\n{schema_description}"
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]

    messages: list[dict] = []
    for turn in history or []:
        messages.append({"role": "user", "content": turn["question"]})
        # Represent the prior assistant turn as the SQL it produced, so the
        # model can see which table/columns the conversation is about.
        messages.append({"role": "assistant", "content": turn["sql"]})
    messages.append({"role": "user", "content": question})
    if failed_attempt:
        messages.append({"role": "assistant", "content": failed_attempt["sql"]})
        messages.append({
            "role": "user",
            "content": (
                "The database rejected that SQL with this error:\n"
                f"{failed_attempt['error']}\n\n"
                "Fix the query so it runs on this database, following the "
                "dialect rules exactly, and answer the original question."
            ),
        })

    if _provider() == "openai":
        system_text = "\n\n".join(block["text"] for block in system)
        # Local models (esp. reasoning ones) can need room beyond the JSON.
        text = _openai_chat(system_text, messages, 4000, json_schema=_SQL_SCHEMA)
        return _parse_json(text)

    response = _anthropic_client().messages.create(
        model=_model(),
        max_tokens=2000,
        thinking={"type": "adaptive"},
        system=system,
        messages=messages,
        output_config={"format": {"type": "json_schema", "schema": _SQL_SCHEMA}},
    )

    # Surface cache activity in the server log so hits are verifiable.
    u = response.usage
    print(
        f"[generate_sql] cache_read={u.cache_read_input_tokens} "
        f"cache_write={u.cache_creation_input_tokens} "
        f"uncached_input={u.input_tokens} output={u.output_tokens}",
        flush=True,
    )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError("Model returned no SQL.")
    return json.loads(text)


def summarize_answer(
    question: str,
    sql: str,
    columns: list[str],
    rows: list[list],
    max_rows_shown: int = 5,
    has_chart: bool = False,
    dialect: str = "SQL",
) -> str:
    """Produce a natural-language answer from the query results.

    If ``has_chart`` is True, a real chart is rendered separately in the UI, so
    the answer should be a concise textual summary and must not draw ASCII
    charts or disclaim an inability to render graphics.
    """
    shown = rows[:max_rows_shown]
    result_payload = {
        "columns": columns,
        "rows": shown,
        "row_count": len(rows),
        "truncated": len(rows) > len(shown),
    }

    system = (
        f"You answer the user's question about their {dialect} database using the "
        "SQL query that was run and its results. Be concise and direct. Lead "
        "with the answer. If the result is a single number, state it plainly. "
        "If it's a list, present it clearly (a short list or small table). Do "
        "not invent data beyond the results provided.\n"
        "If you present result rows as a table, include AT MOST 5 rows. When "
        "row_count exceeds 5, show the first 5 and add a note like 'Showing 5 "
        "of N rows — open \"Show SQL & data\" for the full result.'"
    )
    if has_chart:
        system += (
            " A chart visualizing this result is rendered separately in the UI, "
            "so do NOT draw ASCII/text charts and do NOT say you cannot render "
            "graphics. Give a brief textual summary of what the chart shows "
            "(key figures, shares, or the trend)."
        )
    user = (
        f"Question: {question}\n\n"
        f"SQL executed:\n{sql}\n\n"
        f"Results (JSON):\n{json.dumps(result_payload, default=str)}"
    )

    messages = [{"role": "user", "content": user}]
    if _provider() == "openai":
        return _strip_reasoning(_openai_chat(system, messages, 4000))

    response = _anthropic_client().messages.create(
        model=_model(),
        max_tokens=2000,
        system=system,
        messages=messages,
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()

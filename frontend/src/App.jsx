import { useState, useRef, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Chart from "./Chart.jsx";

// Render the assistant answer as Markdown so tables, bold, and lists display
// properly instead of showing raw `|`/`**` syntax. GFM adds table support.
// Tables are wrapped in a scroll container so long cells (e.g. filenames)
// don't blow out the layout.
const MD_COMPONENTS = {
  table: (props) => (
    <div className="md-table-wrap">
      <table {...props} />
    </div>
  ),
};

function Answer({ text }) {
  return (
    <div className="answer">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

// Copy text to the clipboard, briefly flipping the label to confirm.
function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard can be unavailable (e.g. non-HTTPS origin); the SQL is still
      // selectable in the block below.
    }
  }

  return (
    <button className="copy" onClick={copy}>
      {copied ? "Copied" : "Copy SQL"}
    </button>
  );
}

// A statement that would modify the database. The backend never executes
// these; show the SQL prominently so the user can copy and run it themselves.
function WriteStatement({ msg }) {
  if (msg.executed === false) {
    return <WriteStatement msg={msg} />;
  }

  return (
    <div className="msg assistant">
      <div className="bubble">
        <Answer text={msg.answer} />
        <div className="warning" role="alert">
          <strong>Not executed.</strong> {msg.warning}
        </div>
        <div className="sql-head">
          <span className="label">SQL for {msg.databaseLabel}</span>
          <CopyButton text={msg.sql} />
        </div>
        <pre className="sql">{msg.sql}</pre>
      </div>
    </div>
  );
}

// A single chat turn. `role` is "user" or "assistant". Assistant turns carry
// the answer text plus the SQL/rows metadata for the expandable detail panel.
function Message({ msg }) {
  const [showDetails, setShowDetails] = useState(false);

  if (msg.role === "user") {
    return (
      <div className="msg user">
        <div className="bubble">{msg.text}</div>
      </div>
    );
  }

  if (msg.error) {
    return (
      <div className="msg assistant">
        <div className="bubble error">
          <strong>Error:</strong>
          <pre>{msg.text}</pre>
        </div>
      </div>
    );
  }

  if (msg.executed === false) {
    return <WriteStatement msg={msg} />;
  }

  return (
    <div className="msg assistant">
      <div className="bubble">
        <Answer text={msg.answer} />
        {msg.chart && <Chart chart={msg.chart} />}
        <button className="toggle" onClick={() => setShowDetails((v) => !v)}>
          {showDetails ? "Hide details" : "Show SQL & data"}
        </button>
        {showDetails && (
          <div className="details">
            <div className="sql-head">
              <span className="label">Generated SQL ({msg.databaseLabel})</span>
              <CopyButton text={msg.sql} />
            </div>
            <pre className="sql">{msg.sql}</pre>
            {msg.columns?.length > 0 && (
              <>
                <div className="label">
                  Rows returned: {msg.row_count}
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        {msg.columns.map((c) => (
                          <th key={c}>{c}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {msg.rows.slice(0, 100).map((row, i) => (
                        <tr key={i}>
                          {row.map((cell, j) => (
                            <td key={j}>{cell === null ? "∅" : String(cell)}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [databases, setDatabases] = useState([]);
  const [database, setDatabase] = useState("");
  const [dbError, setDbError] = useState(null);
  const scrollRef = useRef(null);

  // Load the configured databases for the selector.
  useEffect(() => {
    fetch("/api/databases")
      .then(async (res) => {
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
        setDatabases(data.databases);
        setDatabase(data.default);
      })
      .catch((err) => setDbError(String(err.message || err)));
  }, []);

  const current = databases.find((d) => d.name === database);

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [messages, loading]);

  async function send() {
    const question = input.trim();
    if (!question || loading) return;

    // Build conversation history from prior turns so the backend can resolve
    // references (an omitted table name, "that table") to context. `messages`
    // here is the pre-update value — exactly the prior turns. Pair each user
    // question with the SQL of the assistant turn that answered it, keep the
    // last 8 turns to bound token cost. Only turns against the currently
    // selected database count: SQL for another database (possibly another
    // dialect) would mislead the model.
    const history = [];
    let lastQuestion = null;
    for (const m of messages) {
      if (m.role === "user") lastQuestion = m.text;
      else if (m.role === "assistant" && m.sql && lastQuestion != null) {
        if (m.database === database) {
          history.push({ question: lastQuestion, sql: m.sql });
        }
        lastQuestion = null;
      }
    }
    const recentHistory = history.slice(-8);

    setMessages((m) => [...m, { role: "user", text: question }]);
    setInput("");
    setLoading(true);

    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, database, history: recentHistory }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || `Request failed (${res.status})`);
      }
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          answer: data.answer,
          sql: data.sql,
          database: data.database,
          databaseLabel:
            databases.find((d) => d.name === data.database)?.label ??
            data.database,
          executed: data.executed,
          warning: data.warning,
          columns: data.columns,
          rows: data.rows,
          row_count: data.row_count,
          chart: data.chart,
        },
      ]);
    } catch (err) {
      setMessages((m) => [
        ...m,
        { role: "assistant", error: true, text: String(err.message || err) },
      ]);
    } finally {
      setLoading(false);
    }
  }

  function onKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <div className="app">
      <header>
        <div className="header-row">
          <h1>DB Chatbot</h1>
          <label className="db-select">
            Database
            <select
              value={database}
              onChange={(e) => setDatabase(e.target.value)}
              disabled={loading || databases.length === 0}
            >
              {databases.map((d) => (
                <option key={d.name} value={d.name}>
                  {d.label} ({d.dialect})
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className="sub">
          Ask a question in plain English — it becomes SQL. Requests to change
          the database produce SQL for you to run yourself; the chatbot only
          executes read-only queries.
        </p>
        {dbError && (
          <p className="db-error">Could not load databases: {dbError}</p>
        )}
      </header>

      <div className="chat" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="empty">
            <p>Try asking:</p>
            <ul>
              <li>How many rows are in AIRS_ARCHIVED_L2_BIN_MET?</li>
              <li>
                How many distinct collections are in AIRS_ARCHIVED_L2_BIN_MET?
              </li>
            </ul>
          </div>
        )}
        {messages.map((msg, i) => (
          <Message key={i} msg={msg} />
        ))}
        {loading && (
          <div className="msg assistant">
            <div className="bubble typing">Thinking…</div>
          </div>
        )}
      </div>

      <div className="composer">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={
            current ? `Ask about ${current.label}…` : "Ask about your database…"
          }
          rows={2}
        />
        <button
          onClick={send}
          disabled={loading || !input.trim() || !database}
        >
          Send
        </button>
      </div>
    </div>
  );
}

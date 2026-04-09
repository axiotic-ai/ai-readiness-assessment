#!/usr/bin/env python3
"""
TALOS AI Readiness Assessment — Backend Server
Receives and stores assessment submissions from the TALOS HTML tool.

Usage:
    pip install -r requirements.txt
    python assessment_server.py

Endpoints:
    GET  /                     — Assessment UI
    GET  /health               — Service metadata / health check
    POST /api/submit           — Submit assessment results
    GET  /api/submissions      — List all submissions (JSON)
    GET  /api/submissions/csv  — Download all submissions as CSV
    GET  /admin                — Admin dashboard
"""

import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    render_template_string,
    request,
    send_from_directory,
)
from flask_cors import CORS

# ── Configuration ────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "submissions.db"
PORT = int(os.environ.get("TALOS_PORT", 5050))

app = Flask(__name__)
CORS(app)


# ── Database ─────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    """Get or create a database connection for the current request."""
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc: Exception | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    """Create the submissions table if it doesn't exist."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            respondent_name TEXT    DEFAULT '',
            respondent_email TEXT   DEFAULT '',
            organisation    TEXT    DEFAULT '',
            submitted_at    TEXT    NOT NULL,
            overall_score   REAL    NOT NULL,
            category_scores_json TEXT NOT NULL,
            answers_json    TEXT    NOT NULL,
            received_at     TEXT    NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


# ── Question labels (for CSV column headers) ────────────────────
CATEGORY_NAMES = [
    "Foundational Readiness",
    "People & Skills",
    "Platform & Technology",
    "Process & Operations",
    "Strategic & Transformational",
]

QUESTIONS_PER_CATEGORY = [5, 5, 5, 5, 6]  # 26 total


# ── Routes ───────────────────────────────────────────────────────
@app.route("/api/submit", methods=["POST"])
def submit():
    """Accept a JSON assessment submission."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    # Required fields
    overall_score = data.get("overall_score")
    submitted_at = data.get("submitted_at")
    category_scores = data.get("category_scores", {})
    answers_data = data.get("answers", {})

    if overall_score is None or submitted_at is None:
        return jsonify({"error": "Missing required fields: overall_score, submitted_at"}), 400

    # Optional fields
    respondent_name = str(data.get("respondent_name", "") or "")
    respondent_email = str(data.get("respondent_email", "") or "")
    organisation = str(data.get("organisation", "") or "")

    received_at = datetime.now(timezone.utc).isoformat()

    db = get_db()
    db.execute(
        """
        INSERT INTO submissions
            (respondent_name, respondent_email, organisation,
             submitted_at, overall_score, category_scores_json,
             answers_json, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            respondent_name,
            respondent_email,
            organisation,
            submitted_at,
            float(overall_score),
            json.dumps(category_scores),
            json.dumps(answers_data),
            received_at,
        ),
    )
    db.commit()

    return jsonify({"status": "ok", "message": "Submission received"}), 201


@app.route("/api/submissions", methods=["GET"])
def list_submissions():
    """Return all submissions as JSON."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM submissions ORDER BY id DESC"
    ).fetchall()

    result = []
    for row in rows:
        result.append(
            {
                "id": row["id"],
                "respondent_name": row["respondent_name"],
                "respondent_email": row["respondent_email"],
                "organisation": row["organisation"],
                "submitted_at": row["submitted_at"],
                "overall_score": row["overall_score"],
                "category_scores": json.loads(row["category_scores_json"]),
                "answers": json.loads(row["answers_json"]),
                "received_at": row["received_at"],
            }
        )

    return jsonify(result)


@app.route("/api/submissions/csv", methods=["GET"])
def download_csv():
    """Return all submissions as a downloadable CSV."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM submissions ORDER BY id ASC"
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)

    # Build header
    header = [
        "ID",
        "Name",
        "Email",
        "Organisation",
        "Submitted At",
        "Received At",
        "Overall Score (%)",
    ]

    # Category score columns
    for cat_name in CATEGORY_NAMES:
        header.append(f"{cat_name} (%)")

    # Individual question columns
    for cat_idx, cat_name in enumerate(CATEGORY_NAMES):
        for q_idx in range(QUESTIONS_PER_CATEGORY[cat_idx]):
            header.append(f"{cat_name} Q{q_idx + 1}")

    writer.writerow(header)

    # Data rows
    for row in rows:
        cat_scores = json.loads(row["category_scores_json"])
        answers_data = json.loads(row["answers_json"])

        csv_row = [
            row["id"],
            row["respondent_name"],
            row["respondent_email"],
            row["organisation"],
            row["submitted_at"],
            row["received_at"],
            round(row["overall_score"], 1),
        ]

        # Category scores
        for cat_name in CATEGORY_NAMES:
            csv_row.append(round(cat_scores.get(cat_name, 0), 1))

        # Individual answers
        for cat_idx in range(len(CATEGORY_NAMES)):
            for q_idx in range(QUESTIONS_PER_CATEGORY[cat_idx]):
                key = f"{cat_idx}-{q_idx}"
                csv_row.append(answers_data.get(key, ""))

        writer.writerow(csv_row)

    csv_data = output.getvalue()
    output.close()

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=talos_submissions_{datetime.now().strftime('%Y%m%d')}.csv"
        },
    )


# ── Admin Dashboard ──────────────────────────────────────────────
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TALOS Admin — Submissions</title>
<style>
  :root {
    --primary: #1a1a2e;
    --accent: #e94560;
    --secondary: #0f3460;
    --bg: #f8f9fa;
    --bg-white: #ffffff;
    --success: #28a745;
    --warning: #ffc107;
    --danger: #dc3545;
    --text: #2d3436;
    --text-light: #636e72;
    --border: #dfe6e9;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 24px;
  }

  .header {
    background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
    color: #fff;
    padding: 24px 32px;
    border-radius: 12px;
    margin-bottom: 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 16px;
  }

  .header h1 { font-size: 22px; letter-spacing: 2px; }
  .header p { font-size: 14px; opacity: 0.8; }

  .btn {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 10px 20px;
    border: none;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    transition: all 0.2s ease;
  }

  .btn-accent {
    background: var(--accent);
    color: #fff;
    box-shadow: 0 2px 8px rgba(233, 69, 96, 0.3);
  }

  .btn-accent:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(233, 69, 96, 0.4);
  }

  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }

  .stat-card {
    background: var(--bg-white);
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 2px 12px rgba(26, 26, 46, 0.06);
    text-align: center;
  }

  .stat-value {
    font-size: 32px;
    font-weight: 800;
    color: var(--primary);
  }

  .stat-label {
    font-size: 13px;
    color: var(--text-light);
    margin-top: 4px;
  }

  .table-wrap {
    background: var(--bg-white);
    border-radius: 12px;
    box-shadow: 0 2px 12px rgba(26, 26, 46, 0.06);
    overflow-x: auto;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
  }

  th {
    background: var(--primary);
    color: #fff;
    padding: 14px 16px;
    text-align: left;
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    white-space: nowrap;
  }

  td {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }

  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(15, 52, 96, 0.02); }

  .score-badge {
    display: inline-block;
    padding: 4px 10px;
    border-radius: 6px;
    font-weight: 700;
    font-size: 13px;
  }

  .score-low { background: #f8d7da; color: var(--danger); }
  .score-mid { background: #fff3cd; color: #856404; }
  .score-high { background: #d4edda; color: #155724; }

  .empty-state {
    text-align: center;
    padding: 60px 20px;
    color: var(--text-light);
  }

  .empty-state p { font-size: 16px; margin-top: 12px; }

  @media (max-width: 640px) {
    body { padding: 12px; }
    .header { flex-direction: column; text-align: center; padding: 20px; }
  }
</style>
</head>
<body>
  <div class="header">
    <div>
      <h1>TALOS Admin</h1>
      <p>Assessment Submissions Dashboard</p>
    </div>
    <a href="/api/submissions/csv" class="btn btn-accent">📥 Download CSV</a>
  </div>

  <div class="stats" id="stats"></div>
  <div class="table-wrap" id="tableWrap"></div>

  <script>
    async function loadData() {
      try {
        const res = await fetch('/api/submissions');
        const data = await res.json();
        renderStats(data);
        renderTable(data);
      } catch (err) {
        document.getElementById('tableWrap').innerHTML =
          '<div class="empty-state"><p>⚠️ Failed to load submissions</p></div>';
      }
    }

    function scoreClass(score) {
      if (score < 35) return 'score-low';
      if (score < 70) return 'score-mid';
      return 'score-high';
    }

    function renderStats(data) {
      const count = data.length;
      const avg = count ? (data.reduce((s, d) => s + d.overall_score, 0) / count) : 0;
      const high = count ? data.filter(d => d.overall_score >= 70).length : 0;

      document.getElementById('stats').innerHTML = `
        <div class="stat-card">
          <div class="stat-value">${count}</div>
          <div class="stat-label">Total Submissions</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${avg.toFixed(1)}%</div>
          <div class="stat-label">Average Score</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${high}</div>
          <div class="stat-label">Transformational (≥70%)</div>
        </div>
      `;
    }

    function renderTable(data) {
      if (!data.length) {
        document.getElementById('tableWrap').innerHTML =
          '<div class="empty-state"><p>No submissions yet. Share the assessment to start collecting results.</p></div>';
        return;
      }

      const cats = [
        'Foundational Readiness',
        'People & Skills',
        'Platform & Technology',
        'Process & Operations',
        'Strategic & Transformational'
      ];

      let html = '<table><thead><tr>';
      html += '<th>#</th><th>Name</th><th>Organisation</th><th>Overall</th>';
      cats.forEach(c => { html += `<th>${c.split(' ')[0]}</th>`; });
      html += '<th>Submitted</th></tr></thead><tbody>';

      data.forEach(row => {
        const cs = row.category_scores || {};
        html += '<tr>';
        html += `<td>${row.id}</td>`;
        html += `<td>${row.respondent_name || '<em style="color:#b2bec3">Anonymous</em>'}</td>`;
        html += `<td>${row.organisation || '—'}</td>`;
        html += `<td><span class="score-badge ${scoreClass(row.overall_score)}">${row.overall_score.toFixed(1)}%</span></td>`;
        cats.forEach(c => {
          const val = cs[c] ?? 0;
          html += `<td><span class="score-badge ${scoreClass(val)}">${val.toFixed(0)}%</span></td>`;
        });
        const d = new Date(row.submitted_at);
        html += `<td>${d.toLocaleDateString('en-GB')} ${d.toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit'})}</td>`;
        html += '</tr>';
      });

      html += '</tbody></table>';
      document.getElementById('tableWrap').innerHTML = html;
    }

    loadData();
  </script>
</body>
</html>
"""


@app.route("/admin")
def admin():
    return render_template_string(ADMIN_HTML)


@app.route("/")
def assessment_ui():
    return send_from_directory(str(BASE_DIR), "index.html")


@app.route("/health")
def health():
    return jsonify(
        {
            "service": "TALOS Assessment Backend",
            "version": "1.0.0",
            "endpoints": {
                "GET /": "Assessment UI",
                "GET /health": "Service metadata / health check",
                "POST /api/submit": "Submit assessment results",
                "GET /api/submissions": "List all submissions (JSON)",
                "GET /api/submissions/csv": "Download submissions as CSV",
                "GET /admin": "Admin dashboard",
            },
        }
    )


# ── Entrypoint ───────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print(f"🎯 TALOS Assessment Server running on http://0.0.0.0:{PORT}")
    print(f"📊 Admin dashboard: http://localhost:{PORT}/admin")
    print(f"💾 Database: {DB_PATH}")
    app.run(host="0.0.0.0", port=PORT, debug=False)

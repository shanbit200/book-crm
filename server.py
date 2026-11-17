#!/usr/bin/env python3
"""Book Repository CRM — Python stdlib server backed by SQLite (local) or Postgres (Heroku).

Run locally:  python3 server.py [--port 8000] [--db books.db]
Then open http://localhost:8000

Environment variables:
  DATABASE_URL          postgres://... — use Postgres instead of SQLite (needs `psycopg`)
  PORT                  port to listen on (set by Heroku)
  BASIC_AUTH_USER       when both are set, every request needs this username/password
  BASIC_AUTH_PASSWORD   (both are required when running on Heroku)
"""
import argparse
import base64
import hmac
import os
import sys
import csv
import io
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_LOCK = threading.Lock()

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    isbn TEXT DEFAULT '',
    genre TEXT DEFAULT '',
    publisher TEXT DEFAULT '',
    year INTEGER,
    copies INTEGER NOT NULL DEFAULT 1 CHECK (copies >= 0),
    location TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    address TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    loaned_on TEXT NOT NULL,
    due_on TEXT NOT NULL,
    returned_on TEXT,
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL DEFAULT (datetime('now')),
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loans_book ON loans(book_id);
CREATE INDEX IF NOT EXISTS idx_loans_member ON loans(member_id);
"""

PG_NOW = "to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')"
PG_SCHEMA = (
    SQLITE_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    .replace("(datetime('now'))", f"({PG_NOW})")
)

BOOK_FIELDS = ["title", "author", "isbn", "genre", "publisher", "year", "copies", "location", "tags", "notes"]
MEMBER_FIELDS = ["name", "email", "phone", "address", "notes"]

BOOK_SELECT = """
SELECT b.*,
       b.copies - COALESCE((SELECT COUNT(*) FROM loans l WHERE l.book_id = b.id AND l.returned_on IS NULL), 0) AS available
FROM books b
"""

MEMBER_SELECT = """
SELECT m.*,
       (SELECT COUNT(*) FROM loans l WHERE l.member_id = m.id AND l.returned_on IS NULL) AS active_loans,
       (SELECT COUNT(*) FROM loans l WHERE l.member_id = m.id) AS total_loans
FROM members m
"""

LOAN_SELECT = """
SELECT l.*, b.title AS book_title, b.author AS book_author, m.name AS member_name, m.email AS member_email
FROM loans l JOIN books b ON b.id = l.book_id JOIN members m ON m.id = l.member_id
"""


def today():
    return date.today().isoformat()


def with_status(loan):
    if loan is not None:
        loan["status"] = (
            "returned" if loan["returned_on"] else "overdue" if loan["due_on"] < today() else "active"
        )
    return loan


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Store:
    """Thin data layer that runs the same SQL on SQLite or Postgres (`?` placeholders are translated)."""

    def __init__(self, path=None, database_url=None):
        self.url = database_url
        self.path = path
        self._conn = None
        if self.url:
            import psycopg  # noqa: F401 — fail fast if missing
            with self.conn.cursor() as cur:
                cur.execute(PG_SCHEMA)
        else:
            self.conn.executescript(SQLITE_SCHEMA)
        self.conn.commit()

    @property
    def conn(self):
        if self.url:
            import psycopg
            from psycopg.rows import dict_row
            if self._conn is None or self._conn.closed or self._conn.broken:
                self._conn = psycopg.connect(self.url, row_factory=dict_row)
        elif self._conn is None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    # -- helpers -----------------------------------------------------------
    def exec(self, sql, args=()):
        if self.url:
            cur = self.conn.cursor()
            cur.execute(sql.replace("?", "%s"), tuple(args))
            return cur
        return self.conn.execute(sql, tuple(args))

    def q(self, sql, args=()):
        return [dict(r) for r in self.exec(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.exec(sql, args).fetchone()
        return dict(r) if r else None

    def insert(self, table, data):
        cols = list(data)
        row = self.exec(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) RETURNING id",
            [data[c] for c in cols],
        ).fetchone()
        return row["id"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]

    def loans_where(self, where="", args=(), order="l.loaned_on DESC, l.id DESC"):
        sql = LOAN_SELECT + (f" WHERE {where}" if where else "") + f" ORDER BY {order}"
        return [with_status(r) for r in self.q(sql, args)]

    def get_loan(self, lid):
        rows = self.loans_where("l.id = ?", (lid,))
        return rows[0] if rows else None

    def rollback(self):
        if self._conn is not None:
            try:
                self._conn.rollback()
            except Exception:
                self._conn = None

    def log(self, kind, message):
        self.exec("INSERT INTO activity (kind, message) VALUES (?, ?)", (kind, message))

    # -- books -------------------------------------------------------------
    def clean_book(self, data, partial=False):
        out = {}
        for f in BOOK_FIELDS:
            if f not in data:
                continue
            v = data[f]
            if f in ("year", "copies"):
                if v in (None, ""):
                    v = None if f == "year" else 1
                else:
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        raise ApiError(400, f"'{f}' must be a whole number")
                    if v < 0:
                        raise ApiError(400, f"'{f}' cannot be negative")
            else:
                v = "" if v is None else str(v).strip()
            out[f] = v
        if not partial and not out.get("title"):
            raise ApiError(400, "Title is required")
        if partial and "title" in out and not out["title"]:
            raise ApiError(400, "Title cannot be empty")
        if out.get("isbn"):
            digits = re.sub(r"[^0-9Xx]", "", out["isbn"])
            if len(digits) not in (10, 13):
                raise ApiError(400, "ISBN must have 10 or 13 digits")
        return out

    def list_books(self, search="", genre="", availability=""):
        where, args = [], []
        if search:
            like = f"%{search.lower()}%"
            where.append("(LOWER(b.title) LIKE ? OR LOWER(b.author) LIKE ? OR LOWER(b.isbn) LIKE ?"
                         " OR LOWER(b.tags) LIKE ? OR LOWER(b.publisher) LIKE ?)")
            args += [like] * 5
        if genre:
            where.append("b.genre = ?")
            args.append(genre)
        sql = BOOK_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY LOWER(b.title)"
        rows = self.q(sql, args)
        if availability == "available":
            rows = [r for r in rows if r["available"] > 0]
        elif availability == "out":
            rows = [r for r in rows if r["available"] <= 0]
        return rows

    def get_book(self, bid):
        b = self.one(BOOK_SELECT + " WHERE b.id = ?", (bid,))
        if not b:
            raise ApiError(404, "Book not found")
        b["loans"] = self.loans_where("l.book_id = ?", (bid,))
        return b

    def create_book(self, data):
        d = self.clean_book(data)
        bid = self.insert("books", d)
        self.log("book", f"Added book “{d['title']}”")
        self.conn.commit()
        return self.get_book(bid)

    def update_book(self, bid, data):
        current = self.get_book(bid)
        d = self.clean_book(data, partial=True)
        if "copies" in d:
            on_loan = current["copies"] - current["available"]
            if d["copies"] < on_loan:
                raise ApiError(400, f"{on_loan} copies are on loan; copies cannot be lower than that")
        if d:
            sets = ", ".join(f"{c} = ?" for c in d)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self.exec(f"UPDATE books SET {sets}, updated_at = ? WHERE id = ?", [*d.values(), now, bid])
            self.log("book", f"Updated book “{d.get('title', current['title'])}”")
            self.conn.commit()
        return self.get_book(bid)

    def delete_book(self, bid):
        b = self.get_book(bid)
        if b["copies"] - b["available"] > 0:
            raise ApiError(409, "Book has copies on loan; return them first")
        self.exec("DELETE FROM books WHERE id = ?", (bid,))
        self.log("book", f"Deleted book “{b['title']}”")
        self.conn.commit()

    def genres(self):
        return [r["genre"] for r in self.q("SELECT DISTINCT genre FROM books WHERE genre != '' ORDER BY genre")]

    # -- members -----------------------------------------------------------
    def clean_member(self, data, partial=False):
        out = {f: ("" if data[f] is None else str(data[f]).strip()) for f in MEMBER_FIELDS if f in data}
        if (not partial or "name" in out) and not out.get("name"):
            raise ApiError(400, "Name is required")
        if out.get("email") and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", out["email"]):
            raise ApiError(400, "Email address looks invalid")
        return out

    def list_members(self, search=""):
        if search:
            like = f"%{search.lower()}%"
            return self.q(
                MEMBER_SELECT + " WHERE LOWER(m.name) LIKE ? OR LOWER(m.email) LIKE ? OR m.phone LIKE ?"
                " ORDER BY LOWER(m.name)",
                (like, like, like),
            )
        return self.q(MEMBER_SELECT + " ORDER BY LOWER(m.name)")

    def get_member(self, mid):
        m = self.one(MEMBER_SELECT + " WHERE m.id = ?", (mid,))
        if not m:
            raise ApiError(404, "Member not found")
        m["loans"] = self.loans_where("l.member_id = ?", (mid,))
        return m

    def create_member(self, data):
        d = self.clean_member(data)
        mid = self.insert("members", d)
        self.log("member", f"Added member {d['name']}")
        self.conn.commit()
        return self.get_member(mid)

    def update_member(self, mid, data):
        current = self.get_member(mid)
        d = self.clean_member(data, partial=True)
        if d:
            sets = ", ".join(f"{c} = ?" for c in d)
            self.exec(f"UPDATE members SET {sets} WHERE id = ?", [*d.values(), mid])
            self.log("member", f"Updated member {d.get('name', current['name'])}")
            self.conn.commit()
        return self.get_member(mid)

    def delete_member(self, mid):
        m = self.get_member(mid)
        if m["active_loans"]:
            raise ApiError(409, "Member still has books on loan")
        self.exec("DELETE FROM members WHERE id = ?", (mid,))
        self.log("member", f"Deleted member {m['name']}")
        self.conn.commit()

    # -- loans -------------------------------------------------------------
    def list_loans(self, status=""):
        rows = self.loans_where(order="CASE WHEN l.returned_on IS NULL THEN 0 ELSE 1 END, l.due_on, l.id DESC")
        if status == "open":
            return [r for r in rows if r["status"] != "returned"]
        if status:
            return [r for r in rows if r["status"] == status]
        return rows

    def create_loan(self, data):
        try:
            bid, mid = int(data.get("book_id")), int(data.get("member_id"))
        except (TypeError, ValueError):
            raise ApiError(400, "Choose a book and a member")
        book = self.get_book(bid)
        member = self.get_member(mid)
        if book["available"] <= 0:
            raise ApiError(409, f"No copies of “{book['title']}” are available")
        loaned = parse_date(data.get("loaned_on")) or date.today()
        due = parse_date(data.get("due_on")) or loaned + timedelta(days=14)
        if due < loaned:
            raise ApiError(400, "Due date must be on or after the loan date")
        lid = self.insert("loans", {
            "book_id": bid, "member_id": mid, "loaned_on": loaned.isoformat(), "due_on": due.isoformat(),
            "notes": str(data.get("notes") or "").strip(),
        })
        self.log("loan", f"Lent “{book['title']}” to {member['name']} (due {due.isoformat()})")
        self.conn.commit()
        return self.get_loan(lid)

    def return_loan(self, lid):
        loan = self.get_loan(lid)
        if not loan:
            raise ApiError(404, "Loan not found")
        if loan["returned_on"]:
            raise ApiError(409, "Loan already returned")
        self.exec("UPDATE loans SET returned_on = ? WHERE id = ?", (today(), lid))
        self.log("loan", f"{loan['member_name']} returned “{loan['book_title']}”")
        self.conn.commit()
        return self.get_loan(lid)

    def extend_loan(self, lid, data):
        loan = self.get_loan(lid)
        if not loan:
            raise ApiError(404, "Loan not found")
        if loan["returned_on"]:
            raise ApiError(409, "Loan already returned")
        due = parse_date(data.get("due_on"))
        if not due:
            try:
                days = int(data.get("days") or 7)
            except (TypeError, ValueError):
                raise ApiError(400, "'days' must be a whole number")
            due = max(date.fromisoformat(loan["due_on"]), date.today()) + timedelta(days=days)
        if due < date.fromisoformat(loan["loaned_on"]):
            raise ApiError(400, "Due date must be on or after the loan date")
        self.exec("UPDATE loans SET due_on = ? WHERE id = ?", (due.isoformat(), lid))
        self.log("loan", f"Extended “{loan['book_title']}” for {loan['member_name']} to {due.isoformat()}")
        self.conn.commit()
        return self.get_loan(lid)

    # -- dashboard ---------------------------------------------------------
    def stats(self):
        s = self.one(
            """SELECT (SELECT COUNT(*) FROM books) AS titles,
                      (SELECT CAST(COALESCE(SUM(copies),0) AS INTEGER) FROM books) AS copies,
                      (SELECT COUNT(*) FROM members) AS members,
                      (SELECT COUNT(*) FROM loans WHERE returned_on IS NULL) AS on_loan,
                      (SELECT COUNT(*) FROM loans WHERE returned_on IS NULL AND due_on < ?) AS overdue""",
            (today(),),
        )
        s["by_genre"] = self.q(
            "SELECT COALESCE(NULLIF(genre,''),'Uncategorised') AS genre, COUNT(*) AS n FROM books GROUP BY 1 ORDER BY n DESC LIMIT 8"
        )
        s["overdue_loans"] = self.list_loans("overdue")[:10]
        s["due_soon"] = [
            r for r in self.list_loans("active")
            if date.fromisoformat(r["due_on"]) <= date.today() + timedelta(days=3)
        ][:10]
        s["activity"] = self.q("SELECT * FROM activity ORDER BY id DESC LIMIT 15")
        return s

    def books_csv(self):
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = ["id", *BOOK_FIELDS, "available"]
        w.writerow(cols)
        for b in self.list_books():
            w.writerow([b[c] if b[c] is not None else "" for c in cols])
        return buf.getvalue()

    def import_books_csv(self, text):
        reader = csv.DictReader(io.StringIO(text))
        added, errors = 0, []
        for i, row in enumerate(reader, start=2):
            row = {k.strip().lower(): v for k, v in row.items() if k}
            try:
                d = self.clean_book({k: row[k] for k in BOOK_FIELDS if k in row})
                self.insert("books", d)
                added += 1
            except ApiError as e:
                errors.append(f"Row {i}: {e.message}")
        if added:
            self.log("book", f"Imported {added} books from CSV")
        self.conn.commit()
        return {"added": added, "errors": errors}


def parse_date(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ApiError(400, f"Invalid date: {v}")


class Handler(BaseHTTPRequestHandler):
    store: Store = None
    server_version = "BookCRM/1.0"

    def log_message(self, fmt, *args):
        print(f"[{datetime.now():%H:%M:%S}] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def send(self, status, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype.startswith("application/json") else body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if "text/csv" in (self.headers.get("Content-Type") or ""):
            return raw.decode("utf-8-sig")
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "Invalid JSON body")

    def route(self, method):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        s = self.store

        if not path.startswith("/api"):
            if method != "GET":
                raise ApiError(405, "Method not allowed")
            return self.static(path)

        parts = path.split("/")[2:]  # after /api
        res = parts[0] if parts else ""
        rid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        sub = parts[2] if len(parts) > 2 else None

        if res == "stats" and method == "GET":
            return s.stats()
        if res == "genres" and method == "GET":
            return s.genres()
        if res == "export" and method == "GET":
            return ("csv", s.books_csv())
        if res == "import" and method == "POST":
            text = self.body()
            if not isinstance(text, str):
                raise ApiError(400, "Send CSV with Content-Type: text/csv")
            return s.import_books_csv(text)
        if res == "books":
            if rid is None and method == "GET":
                return s.list_books(qs.get("q", ""), qs.get("genre", ""), qs.get("availability", ""))
            if rid is None and method == "POST":
                return (201, s.create_book(self.body()))
            if rid and method == "GET":
                return s.get_book(rid)
            if rid and method == "PUT":
                return s.update_book(rid, self.body())
            if rid and method == "DELETE":
                s.delete_book(rid)
                return {"ok": True}
        if res == "members":
            if rid is None and method == "GET":
                return s.list_members(qs.get("q", ""))
            if rid is None and method == "POST":
                return (201, s.create_member(self.body()))
            if rid and method == "GET":
                return s.get_member(rid)
            if rid and method == "PUT":
                return s.update_member(rid, self.body())
            if rid and method == "DELETE":
                s.delete_member(rid)
                return {"ok": True}
        if res == "loans":
            if rid is None and method == "GET":
                return s.list_loans(qs.get("status", ""))
            if rid is None and method == "POST":
                return (201, s.create_loan(self.body()))
            if rid and sub == "return" and method == "POST":
                return s.return_loan(rid)
            if rid and sub == "extend" and method == "POST":
                return s.extend_loan(rid, self.body())
        raise ApiError(404, "Not found")

    def static(self, path):
        target = (STATIC / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        if STATIC not in target.parents or not target.is_file():
            target = STATIC / "index.html"
        ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}.get(
            target.suffix, "application/octet-stream"
        )
        return ("raw", target.read_bytes(), ctype + "; charset=utf-8")

    def authorized(self):
        user, pw = os.environ.get("BASIC_AUTH_USER"), os.environ.get("BASIC_AUTH_PASSWORD")
        if not (user and pw):
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            given_user, _, given_pw = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(given_user.encode(), user.encode()) & hmac.compare_digest(
            given_pw.encode(), pw.encode()
        )

    def handle_method(self, method):
        # Heroku terminates TLS and forwards the original scheme; force HTTPS so the password isn't sent in clear.
        if self.headers.get("X-Forwarded-Proto") == "http":
            self.send(301, b"", "text/plain", {"Location": f"https://{self.headers.get('Host', '')}{self.path}"})
            return
        if not self.authorized():
            self.send(401, {"error": "Authentication required"},
                      headers={"WWW-Authenticate": 'Basic realm="Book CRM", charset="UTF-8"'})
            return
        try:
            with DB_LOCK:
                try:
                    result = self.route(method)
                finally:
                    self.store.rollback()  # no-op after a commit; ends read transactions and clears failed ones
            if isinstance(result, tuple) and result[0] == "raw":
                self.send(200, result[1], result[2])
            elif isinstance(result, tuple) and result[0] == "csv":
                self.send(200, result[1], "text/csv; charset=utf-8",
                          {"Content-Disposition": f'attachment; filename="books-{date.today()}.csv"'})
            elif isinstance(result, tuple):
                self.send(result[0], result[1])
            else:
                self.send(200, result)
        except ApiError as e:
            self.send(e.status, {"error": e.message})
        except Exception as e:  # database or unexpected error
            print(f"ERROR {method} {self.path}: {e!r}", file=sys.stderr)
            self.send(500, {"error": "Server error — please try again"})

    def do_GET(self):
        self.handle_method("GET")

    def do_POST(self):
        self.handle_method("POST")

    def do_PUT(self):
        self.handle_method("PUT")

    def do_DELETE(self):
        self.handle_method("DELETE")


def main():
    ap = argparse.ArgumentParser(description="Book Repository CRM")
    on_heroku = "DYNO" in os.environ
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default="0.0.0.0" if on_heroku else "127.0.0.1")
    ap.add_argument("--db", default=str(ROOT / "books.db"), help="SQLite file (ignored when DATABASE_URL is set)")
    args = ap.parse_args()

    has_auth = bool(os.environ.get("BASIC_AUTH_USER") and os.environ.get("BASIC_AUTH_PASSWORD"))
    if on_heroku and not has_auth:
        sys.exit("Refusing to start: set BASIC_AUTH_USER and BASIC_AUTH_PASSWORD config vars first.")
    if on_heroku and not os.environ.get("DATABASE_URL"):
        sys.exit("Refusing to start: no DATABASE_URL. Add Heroku Postgres — SQLite data is lost on every restart.")

    database_url = os.environ.get("DATABASE_URL")
    Handler.store = Store(path=args.db, database_url=database_url)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    where = "Postgres (DATABASE_URL)" if database_url else args.db
    print(f"Book CRM running at http://{args.host}:{args.port}  (db: {where}, password: {'on' if has_auth else 'off'})",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Book Repository CRM — zero-dependency server (Python stdlib + SQLite).

Run:  python3 server.py [--port 8000] [--db books.db]
Then open http://localhost:8000
"""
import argparse
import csv
import io
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_LOCK = threading.Lock()

SCHEMA = """
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
SELECT l.*, b.title AS book_title, b.author AS book_author, m.name AS member_name, m.email AS member_email,
       CASE WHEN l.returned_on IS NOT NULL THEN 'returned'
            WHEN l.due_on < date('now', 'localtime') THEN 'overdue'
            ELSE 'active' END AS status
FROM loans l JOIN books b ON b.id = l.book_id JOIN members m ON m.id = l.member_id
"""


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Store:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- helpers -----------------------------------------------------------
    def q(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def log(self, kind, message):
        self.conn.execute("INSERT INTO activity (kind, message) VALUES (?, ?)", (kind, message))

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
            like = f"%{search}%"
            where.append("(b.title LIKE ? OR b.author LIKE ? OR b.isbn LIKE ? OR b.tags LIKE ? OR b.publisher LIKE ?)")
            args += [like] * 5
        if genre:
            where.append("b.genre = ?")
            args.append(genre)
        sql = BOOK_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY b.title COLLATE NOCASE"
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
        b["loans"] = self.q(LOAN_SELECT + " WHERE l.book_id = ? ORDER BY l.loaned_on DESC, l.id DESC", (bid,))
        return b

    def create_book(self, data):
        d = self.clean_book(data)
        cols = list(d)
        cur = self.conn.execute(
            f"INSERT INTO books ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [d[c] for c in cols]
        )
        self.log("book", f"Added book “{d['title']}”")
        self.conn.commit()
        return self.get_book(cur.lastrowid)

    def update_book(self, bid, data):
        current = self.get_book(bid)
        d = self.clean_book(data, partial=True)
        if "copies" in d:
            on_loan = current["copies"] - current["available"]
            if d["copies"] < on_loan:
                raise ApiError(400, f"{on_loan} copies are on loan; copies cannot be lower than that")
        if d:
            sets = ", ".join(f"{c} = ?" for c in d)
            self.conn.execute(
                f"UPDATE books SET {sets}, updated_at = datetime('now') WHERE id = ?", [*d.values(), bid]
            )
            self.log("book", f"Updated book “{d.get('title', current['title'])}”")
            self.conn.commit()
        return self.get_book(bid)

    def delete_book(self, bid):
        b = self.get_book(bid)
        if b["copies"] - b["available"] > 0:
            raise ApiError(409, "Book has copies on loan; return them first")
        self.conn.execute("DELETE FROM books WHERE id = ?", (bid,))
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
            like = f"%{search}%"
            return self.q(
                MEMBER_SELECT + " WHERE m.name LIKE ? OR m.email LIKE ? OR m.phone LIKE ? ORDER BY m.name COLLATE NOCASE",
                (like, like, like),
            )
        return self.q(MEMBER_SELECT + " ORDER BY m.name COLLATE NOCASE")

    def get_member(self, mid):
        m = self.one(MEMBER_SELECT + " WHERE m.id = ?", (mid,))
        if not m:
            raise ApiError(404, "Member not found")
        m["loans"] = self.q(LOAN_SELECT + " WHERE l.member_id = ? ORDER BY l.loaned_on DESC, l.id DESC", (mid,))
        return m

    def create_member(self, data):
        d = self.clean_member(data)
        cols = list(d)
        cur = self.conn.execute(
            f"INSERT INTO members ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [d[c] for c in cols]
        )
        self.log("member", f"Added member {d['name']}")
        self.conn.commit()
        return self.get_member(cur.lastrowid)

    def update_member(self, mid, data):
        current = self.get_member(mid)
        d = self.clean_member(data, partial=True)
        if d:
            sets = ", ".join(f"{c} = ?" for c in d)
            self.conn.execute(f"UPDATE members SET {sets} WHERE id = ?", [*d.values(), mid])
            self.log("member", f"Updated member {d.get('name', current['name'])}")
            self.conn.commit()
        return self.get_member(mid)

    def delete_member(self, mid):
        m = self.get_member(mid)
        if m["active_loans"]:
            raise ApiError(409, "Member still has books on loan")
        self.conn.execute("DELETE FROM members WHERE id = ?", (mid,))
        self.log("member", f"Deleted member {m['name']}")
        self.conn.commit()

    # -- loans -------------------------------------------------------------
    def list_loans(self, status=""):
        rows = self.q(LOAN_SELECT + " ORDER BY l.returned_on IS NOT NULL, l.due_on, l.id DESC")
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
        cur = self.conn.execute(
            "INSERT INTO loans (book_id, member_id, loaned_on, due_on, notes) VALUES (?, ?, ?, ?, ?)",
            (bid, mid, loaned.isoformat(), due.isoformat(), str(data.get("notes") or "").strip()),
        )
        self.log("loan", f"Lent “{book['title']}” to {member['name']} (due {due.isoformat()})")
        self.conn.commit()
        return self.one(LOAN_SELECT + " WHERE l.id = ?", (cur.lastrowid,))

    def return_loan(self, lid):
        loan = self.one(LOAN_SELECT + " WHERE l.id = ?", (lid,))
        if not loan:
            raise ApiError(404, "Loan not found")
        if loan["returned_on"]:
            raise ApiError(409, "Loan already returned")
        self.conn.execute("UPDATE loans SET returned_on = ? WHERE id = ?", (date.today().isoformat(), lid))
        self.log("loan", f"{loan['member_name']} returned “{loan['book_title']}”")
        self.conn.commit()
        return self.one(LOAN_SELECT + " WHERE l.id = ?", (lid,))

    def extend_loan(self, lid, data):
        loan = self.one(LOAN_SELECT + " WHERE l.id = ?", (lid,))
        if not loan:
            raise ApiError(404, "Loan not found")
        if loan["returned_on"]:
            raise ApiError(409, "Loan already returned")
        due = parse_date(data.get("due_on"))
        if not due:
            due = max(date.fromisoformat(loan["due_on"]), date.today()) + timedelta(days=int(data.get("days") or 7))
        if due < date.fromisoformat(loan["loaned_on"]):
            raise ApiError(400, "Due date must be on or after the loan date")
        self.conn.execute("UPDATE loans SET due_on = ? WHERE id = ?", (due.isoformat(), lid))
        self.log("loan", f"Extended “{loan['book_title']}” for {loan['member_name']} to {due.isoformat()}")
        self.conn.commit()
        return self.one(LOAN_SELECT + " WHERE l.id = ?", (lid,))

    # -- dashboard ---------------------------------------------------------
    def stats(self):
        s = self.one(
            """SELECT (SELECT COUNT(*) FROM books) AS titles,
                      (SELECT COALESCE(SUM(copies),0) FROM books) AS copies,
                      (SELECT COUNT(*) FROM members) AS members,
                      (SELECT COUNT(*) FROM loans WHERE returned_on IS NULL) AS on_loan,
                      (SELECT COUNT(*) FROM loans WHERE returned_on IS NULL AND due_on < date('now','localtime')) AS overdue"""
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
                cols = list(d)
                self.conn.execute(
                    f"INSERT INTO books ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [d[c] for c in cols]
                )
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

        with DB_LOCK:
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

    def handle_method(self, method):
        try:
            result = self.route(method)
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
        except sqlite3.Error as e:
            self.send(500, {"error": f"Database error: {e}"})

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
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=str(ROOT / "books.db"))
    args = ap.parse_args()
    Handler.store = Store(args.db)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Book CRM running at http://{args.host}:{args.port}  (db: {args.db})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

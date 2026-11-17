# 📚 Book Repository CRM

A lightweight, self-hosted CRM for managing a book collection, the members who borrow from it, and the loans between them.

- **Zero dependencies** — Python 3 standard library + SQLite. No `pip install`, no build step.
- **Single-page web UI** — works on desktop and mobile, follows your system's light/dark mode.
- **Local-first** — all data lives in one SQLite file on your machine.

---

## Features

### Books
- Title, author, ISBN (validated as 10 or 13 digits), genre, publisher, year, number of copies, shelf location, tags and notes
- Search across title, author, ISBN, publisher and tags
- Filter by genre and by availability
- Live availability count (copies minus copies on loan)
- Per-book loan history
- CSV import and export

### Members
- Name, email, phone, address and notes
- Current loans and full borrowing history per member

### Loans
- Lend a book with a lent date and due date (defaults to 14 days)
- Extend a loan by 7 days, or mark it returned
- Automatic **active / overdue / returned** status

### Dashboard
- Totals for titles, copies, members, books on loan and overdue loans
- Overdue loans and loans due in the next 3 days, with one-click return
- Collection breakdown by genre
- Recent activity log

### Safeguards
- A book can't be lent when no copies are free
- A book's copy count can't go below the number currently on loan
- Books and members with open loans can't be deleted

---

## Requirements

- Python **3.8+** (uses only the standard library)
- Any modern browser

## Getting started

```bash
git clone <your-repo-url> book-crm
cd book-crm
python3 server.py
```

Open **http://localhost:8000**.

### Load sample data (optional)

```bash
python3 seed.py
```

Adds 6 books, 3 members and 3 loans (one overdue). Running it again adds duplicates, so delete `books.db` first if you want a clean start.

### Command-line options

| Option | Default | Description |
|---|---|---|
| `--port` | `8000` | Port to listen on |
| `--host` | `127.0.0.1` | Address to bind to |
| `--db` | `books.db` (next to `server.py`) | Path to the SQLite database file |

Example: `python3 server.py --port 9000 --db ~/library.db`

> ⚠️ There is no login. Keep the default `--host 127.0.0.1` unless you put the app behind your own authentication.

---

## CSV import / export

**Export:** *Books → Export CSV* downloads every book with its current availability.

**Import:** *Books → Import CSV* accepts a UTF-8 CSV with a header row. Column names are case-insensitive; only `title` is required.

```csv
title,author,isbn,genre,publisher,year,copies,location,tags,notes
Dune,Frank Herbert,9780441013593,Science Fiction,Ace,1965,2,A1,"classic, space",
Emma,Jane Austen,,Classic,,1815,1,D3,,
```

Rows that fail validation are skipped and listed after the import.

---

## Project structure

```
.
├── server.py          # HTTP server, REST API and SQLite data layer
├── seed.py            # Optional sample data loader
├── static/
│   └── index.html     # Single-page web UI (HTML + CSS + vanilla JS)
├── .claude/
│   └── launch.json    # Dev-server config for Claude Code
├── .gitignore
└── README.md
```

`books.db` is created on first run and is git-ignored, so your data never gets committed.

---

## REST API

All endpoints return JSON; errors look like `{"error": "message"}` with a 4xx status.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/books` | List books. Query: `q`, `genre`, `availability=available\|out` |
| `POST` | `/api/books` | Create a book |
| `GET` | `/api/books/:id` | Book details with loan history |
| `PUT` | `/api/books/:id` | Update a book (partial updates allowed) |
| `DELETE` | `/api/books/:id` | Delete a book |
| `GET` | `/api/members` | List members. Query: `q` |
| `POST` | `/api/members` | Create a member |
| `GET` | `/api/members/:id` | Member details with borrowing history |
| `PUT` | `/api/members/:id` | Update a member |
| `DELETE` | `/api/members/:id` | Delete a member |
| `GET` | `/api/loans` | List loans. Query: `status=open\|active\|overdue\|returned` |
| `POST` | `/api/loans` | Create a loan: `book_id`, `member_id`, optional `loaned_on`, `due_on`, `notes` |
| `POST` | `/api/loans/:id/return` | Mark a loan returned (today) |
| `POST` | `/api/loans/:id/extend` | Extend a loan: `days` (default 7) or an explicit `due_on` |
| `GET` | `/api/stats` | Dashboard totals, overdue / due-soon loans, genres, activity |
| `GET` | `/api/genres` | Distinct genres |
| `GET` | `/api/export` | Download all books as CSV |
| `POST` | `/api/import` | Import books — body is CSV with `Content-Type: text/csv` |

Example:

```bash
curl -X POST localhost:8000/api/books \
  -H 'Content-Type: application/json' \
  -d '{"title":"Dune","author":"Frank Herbert","copies":2}'
```

---

## Backups

All data is in the single database file. To back it up, copy `books.db` while the server is stopped, or export your books via CSV.

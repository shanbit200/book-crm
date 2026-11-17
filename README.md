# 📚 Book Repository CRM

A lightweight, self-hosted CRM for managing a book collection, the members who borrow from it, and the loans between them.

- **No dependencies locally** — Python 3 standard library + SQLite. No `pip install`, no build step.
- **Deploys to Heroku** — uses Heroku Postgres there (one dependency, `psycopg`), with password protection.
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

- Python **3.8+** locally (standard library only); Heroku uses Python 3.13
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

### Environment variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | `postgres://…` — store data in Postgres instead of SQLite (requires `pip install -r requirements.txt`) |
| `PORT` | Port to listen on (Heroku sets this) |
| `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD` | When both are set, the browser asks for this username and password |

> ⚠️ Without `BASIC_AUTH_*` there is no login. Keep the default `--host 127.0.0.1` locally unless you set a password.

---

## Deploy to Heroku

Heroku's filesystem is wiped on every restart, so the app **must** use Heroku Postgres there; it refuses to start on Heroku without `DATABASE_URL` and a password. Heroku has no free tier — an Eco/Basic dyno plus the `essential-0` Postgres plan costs roughly $10–12/month in total.

```bash
heroku login
heroku create <app-name>
heroku addons:create heroku-postgresql:essential-0
heroku config:set BASIC_AUTH_USER=admin BASIC_AUTH_PASSWORD='<choose-a-strong-password>'
git push heroku main
heroku open
```

Optional: load sample data into the Heroku database with `heroku run python3 seed.py`.

Useful commands: `heroku logs --tail` (server log), `heroku pg:backups:capture` and `heroku pg:backups:download` (database backup).

The repository's `app.json` also lets you create the app, database and a generated password in one go from the Heroku dashboard.

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
├── server.py          # HTTP server, REST API and data layer (SQLite or Postgres)
├── seed.py            # Optional sample data loader
├── static/
│   └── index.html     # Single-page web UI (HTML + CSS + vanilla JS)
├── Procfile           # Heroku: how to start the web process
├── requirements.txt   # Heroku: Postgres driver (psycopg)
├── .python-version    # Heroku: Python version
├── app.json           # Heroku: app manifest (Postgres add-on, config vars)
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

Locally, all data is in the single database file: copy `books.db` while the server is stopped, or export your books via CSV. On Heroku, use `heroku pg:backups:capture`.

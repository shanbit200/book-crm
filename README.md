# Book Repository CRM

A small, zero-dependency CRM for managing a book collection, the people who borrow from it, and loans.
Python 3 standard library + SQLite on the back end, a single HTML page on the front end.

## Run

```bash
python3 server.py
```

Open http://localhost:8000. Data is stored in `books.db` next to the server (change with `--db`, port with `--port`).

Optional sample data: `python3 seed.py`

## Features

- **Books** — title, author, ISBN (validated), genre, publisher, year, copies, shelf location, tags, notes. Search, filter by genre/availability, CSV import & export.
- **Members** — contact details, notes, current and historical borrowing.
- **Loans** — lend with due dates (default 14 days), extend (+7 days), return; overdue detection. A book can't be lent when no copies are free, and copies can't be reduced below the number on loan.
- **Dashboard** — totals, overdue and due-soon loans, collection by genre, activity log.

## API

| Method | Path | |
|---|---|---|
| GET/POST | `/api/books` | list (`?q=&genre=&availability=available\|out`) / create |
| GET/PUT/DELETE | `/api/books/:id` | detail (with loan history) / update / delete |
| GET/POST | `/api/members` | list (`?q=`) / create |
| GET/PUT/DELETE | `/api/members/:id` | |
| GET/POST | `/api/loans` | list (`?status=open\|active\|overdue\|returned`) / create |
| POST | `/api/loans/:id/return`, `/api/loans/:id/extend` | |
| GET | `/api/stats`, `/api/genres`, `/api/export` (CSV) | |
| POST | `/api/import` | body: CSV (`Content-Type: text/csv`) with a header row |

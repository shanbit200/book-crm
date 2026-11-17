#!/usr/bin/env python3
"""Load sample books, members and loans: python3 seed.py [--db books.db]"""
import argparse
from datetime import date, timedelta
from server import ROOT, Store

BOOKS = [
    ("Dune", "Frank Herbert", "9780441013593", "Science Fiction", "Ace", 1965, 2, "A1", "classic, space"),
    ("The Hobbit", "J.R.R. Tolkien", "9780547928227", "Fantasy", "Mariner", 1937, 3, "B2", "classic"),
    ("Sapiens", "Yuval Noah Harari", "9780062316097", "History", "Harper", 2011, 1, "C1", "nonfiction"),
    ("Pride and Prejudice", "Jane Austen", "9780141439518", "Classic", "Penguin", 1813, 2, "D3", "romance"),
    ("Clean Code", "Robert C. Martin", "9780132350884", "Technology", "Prentice Hall", 2008, 1, "E1", "programming"),
    ("The Guide", "R.K. Narayan", "9780143039648", "Fiction", "Penguin", 1958, 2, "D1", "india"),
]
MEMBERS = [("Asha Rao", "asha@example.com", "+91 98000 11111"), ("Daniel Kim", "daniel@example.com", "+1 555 0100"),
           ("Meera Iyer", "meera@example.com", "")]

ap = argparse.ArgumentParser(); ap.add_argument("--db", default=str(ROOT / "books.db")); a = ap.parse_args()
s = Store(a.db)
keys = ["title", "author", "isbn", "genre", "publisher", "year", "copies", "location", "tags"]
b = [s.create_book(dict(zip(keys, row)))["id"] for row in BOOKS]
m = [s.create_member({"name": n, "email": e, "phone": p})["id"] for n, e, p in MEMBERS]
t = date.today()
s.create_loan({"book_id": b[0], "member_id": m[0], "loaned_on": t - timedelta(20), "due_on": t - timedelta(6)})
s.create_loan({"book_id": b[2], "member_id": m[1], "loaned_on": t - timedelta(12), "due_on": t + timedelta(2)})
s.create_loan({"book_id": b[1], "member_id": m[2]})
print(f"Seeded {len(b)} books, {len(m)} members, 3 loans into {a.db}")

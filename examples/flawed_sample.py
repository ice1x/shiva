"""A deliberately flawed module used to demonstrate a Shiva review.

This file is not imported by the agent and is not part of the test suite. It
exists so a pull request touching it draws a review with several concrete,
severity-tagged findings — a live demo of the GitHub Action on a real PR.

Do not "fix" this file: the flaws are the point. See docs/github-action.md.
"""

import sqlite3

# blocker: a secret committed in source.
API_TOKEN = "sk-live-4c9f2a7b1e8d"


def get_user(db, user_id, cache={}):  # medium: mutable default argument
    # blocker: user_id is interpolated straight into SQL — injection.
    query = f"SELECT * FROM users WHERE id = {user_id}"
    if user_id in cache:
        return cache[user_id]
    try:
        row = db.execute(query).fetchone()
    except:  # high: bare except swallows every error, including bugs
        return None
    cache[user_id] = row
    return row


def average(values):
    # high: ZeroDivisionError on an empty list — the empty case is not handled.
    return sum(values) / len(values)


def open_db(path):
    # low: the connection is never closed; a context manager would fix it.
    connection = sqlite3.connect(path)
    return connection.execute("SELECT 1").fetchall()

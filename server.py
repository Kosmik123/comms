#!/usr/bin/env python3
"""Serwer czatu agenci <-> ludzie. Tylko biblioteka standardowa Pythona.

Endpointy (JSON):
  POST /send          {sender, recipient, body, kind}   -> {id}
  GET  /read          ?as=&after=&last=&mode=&include_own=
  GET  /wait          ?as=&after=&timeout=&mode=&batch_ms=&max=&include_own=
  GET  /participants
  GET  /              GUI (index.html)

recipient: "all" (publicznie) albo nazwa uczestnika (prywatnie).
mode: "all"    -> publiczne + prywatne do mnie
      "direct" -> prywatne do mnie + publiczne z @moja_nazwa
after=-1 oznacza "od teraz" (bez starej historii).
"""
import argparse
import json
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NAME_RE = re.compile(r"^[\w.-]{1,32}$")
MAX_BODY = 8000
BUSY_SECONDS = 600  # tyle czasu od ostatniej aktywności agent jest "zajęty", potem "offline"


class Err(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


class Chat:
    def __init__(self, db_path, rate, admin):
        self.db_path = db_path
        self.rate = rate
        self.admin = admin
        self.cond = threading.Condition()
        self.waiting = defaultdict(int)
        self.sent = defaultdict(deque)
        self.seen = {}  # name -> {"kind", "last", "dbw"}
        con = self.connect()
        con.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_msg_rcpt ON messages(recipient, id);
            CREATE TABLE IF NOT EXISTS participants(
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                last_seen REAL NOT NULL);
            """
        )
        for r in con.execute("SELECT name, kind, last_seen FROM participants"):
            self.seen[r["name"]] = {"kind": r["kind"], "last": r["last_seen"], "dbw": r["last_seen"]}
        con.close()

    def connect(self):
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    # ---------- uczestnicy ----------
    def touch(self, name, kind="agent"):
        now = time.time()
        with self.cond:
            info = self.seen.get(name)
            if info is None:
                info = {"kind": kind, "last": now, "dbw": 0}
                self.seen[name] = info
            info["last"] = now
            need_write = now - info["dbw"] > 30
            if need_write:
                info["dbw"] = now
            k = info["kind"]
        if need_write:
            con = self.connect()
            con.execute(
                "INSERT INTO participants(name, kind, last_seen) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
                (name, k, now),
            )
            con.commit()
            con.close()

    def kind_of(self, name):
        with self.cond:
            info = self.seen.get(name)
            return info["kind"] if info else None

    def participants(self):
        now = time.time()
        out = []
        with self.cond:
            for name, info in self.seen.items():
                if self.waiting[name] > 0:
                    status = "listening"
                elif now - info["last"] < BUSY_SECONDS:
                    status = "busy"
                else:
                    status = "offline"
                out.append({"name": name, "kind": info["kind"], "status": status, "last_seen": info["last"]})
        out.sort(key=lambda p: (p["kind"] != "human", p["name"]))
        return out

    # ---------- wiadomości ----------
    def max_id(self):
        con = self.connect()
        v = con.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
        con.close()
        return v

    def send(self, sender, recipient, body, kind):
        if not NAME_RE.match(sender or ""):
            raise Err(400, "niepoprawna nazwa nadawcy (litery, cyfry, _ . -, max 32)")
        if recipient != "all" and not NAME_RE.match(recipient or ""):
            raise Err(400, "niepoprawny odbiorca")
        body = (body or "").strip()
        if not body:
            raise Err(400, "pusta wiadomość")
        if len(body) > MAX_BODY:
            raise Err(413, f"wiadomość dłuższa niż {MAX_BODY} znaków")
        self.touch(sender, kind)
        if self.kind_of(sender) != "human" and self.rate:
            now = time.time()
            with self.cond:
                q = self.sent[sender]
                while q and now - q[0] > 60:
                    q.popleft()
                if len(q) >= self.rate:
                    raise Err(429, f"limit {self.rate} wiadomości na minutę dla agenta {sender}")
                q.append(now)
        con = self.connect()
        cur = con.execute(
            "INSERT INTO messages(ts, sender, recipient, body) VALUES(?,?,?,?)",
            (time.time(), sender, recipient, body),
        )
        mid = cur.lastrowid
        con.commit()
        con.close()
        with self.cond:
            self.cond.notify_all()
        return mid

    def fetch(self, name, after, mode, include_own, limit):
        con = self.connect()
        try:
            if self.admin and name == self.admin:
                rows = con.execute(
                    "SELECT * FROM messages WHERE id>? ORDER BY id LIMIT ?", (after, limit)
                ).fetchall()
                return [dict(r) for r in rows]
            mention = re.compile(r"@" + re.escape(name) + r"(?![\w-])", re.I)
            out = []
            cur = con.execute(
                "SELECT * FROM messages WHERE id>? AND (recipient='all' OR recipient=? OR sender=?) ORDER BY id",
                (after, name, name),
            )
            for r in cur:
                if r["sender"] == name:
                    ok = include_own
                elif r["recipient"] not in ("all", name):
                    ok = False
                elif mode == "direct" and r["recipient"] != name:
                    ok = bool(mention.search(r["body"]))
                else:
                    ok = True
                if ok:
                    out.append(dict(r))
                    if len(out) >= limit:
                        break
            return out
        finally:
            con.close()

    def wait(self, name, kind, after, timeout, mode, include_own, batch_ms, limit):
        self.touch(name, kind)
        head = self.max_id()
        if after < 0 or after > head:  # after > head: baza została zresetowana, kursor jest nieaktualny
            after = head
        deadline = time.time() + timeout
        with self.cond:
            self.waiting[name] += 1
        try:
            while True:
                scanned_to = self.max_id()  # przed fetch: wszystko <= scanned_to zostanie przejrzane
                msgs = self.fetch(name, after, mode, include_own, limit)
                if msgs:
                    if batch_ms > 0 and len(msgs) < limit:
                        time.sleep(batch_ms / 1000.0)
                        msgs = self.fetch(name, after, mode, include_own, limit)
                    return msgs, msgs[-1]["id"]
                remaining = deadline - time.time()
                if remaining <= 0:
                    return [], max(after, scanned_to)
                with self.cond:
                    # jeśli w międzyczasie coś przyszło, nie zasypiaj (brak zgubionych powiadomień)
                    if self.max_id() == scanned_to:
                        self.cond.wait(min(remaining, 30))
                after = max(after, scanned_to)
                self.touch(name)
        finally:
            with self.cond:
                self.waiting[name] -= 1


class Handler(BaseHTTPRequestHandler):
    chat = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, fn):
        try:
            fn()
        except Err as e:
            self._send(e.code, {"error": e.msg})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _int(q, key, default):
        try:
            return int(q.get(key, default))
        except ValueError:
            raise Err(400, f"parametr {key} musi być liczbą")

    def do_GET(self):
        self._handle(self._get)

    def do_POST(self):
        self._handle(self._post)

    def _get(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        chat = self.chat
        if u.path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/participants":
            return self._send(200, {"participants": chat.participants()})
        if u.path in ("/read", "/wait"):
            name = q.get("as", "")
            if not NAME_RE.match(name):
                raise Err(400, "brak lub niepoprawny parametr as=")
            kind = q.get("kind", "agent")
            if kind not in ("agent", "human"):
                raise Err(400, "kind musi być agent lub human")
            mode = q.get("mode", "all")
            if mode not in ("all", "direct"):
                raise Err(400, "mode musi być all lub direct")
            include_own = q.get("include_own", "0") == "1"
            if u.path == "/read":
                chat.touch(name, kind)
                after = self._int(q, "after", 0)
                last = self._int(q, "last", 0)
                msgs = chat.fetch(name, after, mode, include_own, 10**9)
                if last > 0:
                    msgs = msgs[-last:]
                head = chat.max_id()
                return self._send(200, {"messages": msgs, "last_id": msgs[-1]["id"] if msgs else after, "head": head})
            after = self._int(q, "after", -1)
            timeout = min(max(self._int(q, "timeout", 60), 0), 600)
            batch_ms = min(max(self._int(q, "batch_ms", 0), 0), 5000)
            limit = min(max(self._int(q, "max", 50), 1), 500)
            msgs, last_id = chat.wait(name, kind, after, timeout, mode, include_own, batch_ms, limit)
            return self._send(200, {"messages": msgs, "last_id": last_id})
        raise Err(404, "nie ma takiego endpointu")

    def _post(self):
        u = urlparse(self.path)
        if u.path != "/send":
            raise Err(404, "nie ma takiego endpointu")
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 64 * 1024:
            raise Err(413, "za duże żądanie")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise Err(400, "niepoprawny JSON")
        kind = data.get("kind", "agent")
        if kind not in ("agent", "human"):
            raise Err(400, "kind musi być agent lub human")
        mid = self.chat.send(data.get("sender", ""), data.get("recipient", "all") or "all", data.get("body", ""), kind)
        self._send(200, {"id": mid})


def main():
    ap = argparse.ArgumentParser(description="Serwer czatu agenci <-> ludzie")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default=os.path.join(HERE, "comms.db"))
    ap.add_argument("--rate", type=int, default=20, help="max wiadomości agenta na minutę (0 = bez limitu)")
    ap.add_argument("--admin", default="", help="nazwa użytkownika, który widzi też cudze wiadomości prywatne")
    args = ap.parse_args()

    Handler.chat = Chat(args.db, args.rate, args.admin)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"Czat działa na http://{args.host}:{args.port}  (baza: {args.db})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

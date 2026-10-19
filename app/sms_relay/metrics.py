"""Aggregate delivery health on a private monitoring port; never message data."""
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from sms_relay.config import settings


def collect():
    with sqlite3.connect(f'file:{settings.db_path}?mode=ro', uri=True, timeout=2) as db:
        age = db.execute("SELECT coalesce(max((julianday('now')-julianday(created_at))*86400),0) FROM messages WHERE direction='outbound' AND status IN ('queued','sending')").fetchone()[0]
        failed = db.execute("SELECT count(*) FROM messages WHERE direction='outbound' AND status='failed' AND updated_at >= datetime('now','-1 hour')").fetchone()[0]
        fallback = db.execute("SELECT count(*) FROM messages WHERE transport='sms-fallback' AND created_at >= datetime('now','-1 hour')").fetchone()[0]
    return f'home_relay_oldest_pending_seconds {max(0, age)}\nhome_relay_failed_last_hour {failed}\nhome_relay_fallback_last_hour {fallback}\n'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/metrics':
            self.send_error(404); return
        try:
            data = collect().encode()
        except Exception:
            self.send_error(503); return
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args): pass


def start():
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('SMS_RELAY_METRICS_PORT', '9105'))), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

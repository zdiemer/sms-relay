import sqlite3
from dataclasses import replace
from sms_relay import metrics

def test_delivery_health_excludes_old_failures_and_message_contents(tmp_path, monkeypatch):
    path = tmp_path / 'messages.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE messages (direction TEXT,status TEXT,transport TEXT,created_at TEXT,updated_at TEXT,body TEXT)')
        db.execute("INSERT INTO messages VALUES ('outbound','failed','sms',datetime('now','-1 day'),datetime('now','-1 day'),'private')")
        db.execute("INSERT INTO messages VALUES ('outbound','queued','ntfy',datetime('now','-20 minutes'),datetime('now'),'private')")
        db.execute("INSERT INTO messages VALUES ('outbound','failed','sms-fallback',datetime('now'),datetime('now'),'private')")
    monkeypatch.setattr(metrics, 'settings', replace(metrics.settings, db_path=str(path)))
    output = metrics.collect()
    values = dict(line.split() for line in output.splitlines())
    assert float(values['home_relay_oldest_pending_seconds']) >= 1199
    assert values['home_relay_failed_last_hour'] == '1'
    assert values['home_relay_fallback_last_hour'] == '1'
    assert 'private' not in output

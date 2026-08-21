"""LAN-only admin app (§7).

Network posture: waitress binds 127.0.0.1 and nginx exposes it to the LAN
interface only (deploy/nginx-admin.conf). LAN-only blocks outside
CONNECTIONS; it does not stop a hostile public page from making the admin's
own browser POST here. So every mutation additionally requires BOTH:

  - strict Origin/Host validation (missing or mismatched → rejected), and
  - a custom header (X-Admin-Request: 1), which forces a CORS preflight a
    simple cross-origin form post cannot satisfy.

Single-admin by design; no further auth machinery (§11). The approve button
is disabled in the UI without a qualifying eligible attempt — and the same
condition is independently enforced here AND by the Worker (§7).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, render_template, request

from . import NORMALIZATION_VERSION
from .allowlist import AllowlistConflict, uuid_present
from .config import load_config
from .db import connect
from .httpjson import TransportError
from .matcher import ELIGIBILITY_LOOKBACK_S
from .outbox import DecisionOutbox, OutboxBusy, OutboxError
from .worker_client import WorkerClient, WorkerError

logger = logging.getLogger(__name__)


def _origin_host(origin: str) -> str:
    return urlsplit(origin).netloc.lower()


def create_app(
    *,
    worker: WorkerClient,
    backend,
    db_path: str,
    allowed_origins: list[str],
    reviewer: str,
    clock=time.time,
) -> Flask:
    app = Flask(__name__)

    @app.template_filter("timestamp")
    def _fmt_ts(v):
        if not v:
            return "—"
        return datetime.fromtimestamp(int(v)).strftime("%Y-%m-%d %H:%M")

    allowed = [o.rstrip("/") for o in allowed_origins]
    allowed_hosts = {_origin_host(o) for o in allowed} | {"127.0.0.1", "localhost"}
    allowed_hosts |= {f"{h}:8080" for h in ("127.0.0.1", "localhost")}

    def db():
        if "db" not in g:
            g.db = connect(db_path)
        return g.db

    def make_outbox() -> DecisionOutbox:
        # Per-request: sqlite connections are not shared across waitress
        # threads. The shared `backend` keeps the file-tier write mutex.
        return DecisionOutbox(
            db(), worker, backend, default_reviewer=reviewer, clock=clock
        )

    @app.teardown_appcontext
    def _close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.before_request
    def _guards():
        host = (request.host or "").lower()
        if host not in allowed_hosts and host.split(":")[0] not in allowed_hosts:
            return jsonify({"error": "host_not_allowed", "host": host}), 403
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if request.headers.get("X-Admin-Request") != "1":
                return jsonify({"error": "missing_admin_header"}), 403
            origin = request.headers.get("Origin")
            if not origin or origin.rstrip("/") not in allowed:
                return jsonify({"error": "origin_rejected", "origin": origin}), 403
        return None

    @app.after_request
    def _headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
        )
        return resp

    # ------------------------------------------------------------- helpers

    def outbox_map(conn):
        return {
            r["submission_id"]: dict(r)
            for r in conn.execute("SELECT * FROM outbox").fetchall()
        }

    def attempt_by_event(conn, event_id):
        if not event_id:
            return None
        row = conn.execute(
            "SELECT * FROM attempts WHERE event_id = ?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    def candidate_attempts(conn, sub):
        """All local attempts sharing the submission's match key, annotated
        with eligibility — old ones stay visible for troubleshooting but can
        verify nothing (§3)."""
        rows = conn.execute(
            """SELECT * FROM attempts WHERE platform = ? AND normalized = ?
               ORDER BY seen_at DESC LIMIT 10""",
            (sub["platform"], sub["normalized"]),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["eligible"] = (
                d["outcome"] == "whitelist_rejected"
                and sub["created_at"] - ELIGIBILITY_LOOKBACK_S
                <= d["seen_at"]
                <= sub.get("expires_at", float("inf"))
            )
            out.append(d)
        return out

    def honest_state(row) -> str:
        """§7: the UI reports honestly at each stage; if mutation or
        read-back failed, it does not report success."""
        state = row["state"]
        if state == "complete":
            return "Done: applied and recorded."
        if state in ("server_applied", "writeback_pending"):
            return "Approved on server; status/email writeback pending (retrying)."
        if state == "requested":
            if row["last_error"]:
                return f"NOT applied yet: {row['last_error']} (retrying)"
            return "Queued."
        if state == "abandoned":
            return "Abandon in progress."
        return state

    def load_review():
        data = worker.get_review()
        conn = db()
        omap = outbox_map(conn)
        for sub in data.get("submissions", []):
            sub["attempt"] = attempt_by_event(conn, sub.get("attempt_event_id"))
            sub["candidates"] = candidate_attempts(conn, sub)
            ob = omap.get(sub["id"])
            sub["outbox"] = ob
            sub["outbox_msg"] = honest_state(ob) if ob else None
            sub["approvable"] = (
                sub["status"] == "verified"
                and bool(sub.get("uuid"))
                and bool(sub.get("attempt_event_id"))
                and ob is None
            )
            sub["version_ok"] = sub.get("normalization_version") == NORMALIZATION_VERSION
        for sub in data.get("recent_terminal", []):
            ob = omap.get(sub["id"])
            sub["outbox_msg"] = honest_state(ob) if ob else None
        return data

    def verified_sub_or_error(sid: int):
        review = worker.get_review()
        for sub in review.get("submissions", []):
            if sub["id"] == sid:
                return sub, None
        return None, (jsonify({"error": "unknown_or_decided", "submission_id": sid}), 404)

    # --------------------------------------------------------------- pages

    @app.get("/")
    def review_page():
        try:
            data = load_review()
        except (WorkerError, TransportError) as e:
            return render_template("error.html", message=f"Worker unreachable: {e}"), 502
        return render_template("review.html", data=data, reviewer=reviewer)

    @app.get("/attempts")
    def attempts_page():
        conn = db()
        recent = [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM attempts WHERE seen_at >= ?
                   ORDER BY seen_at DESC LIMIT 200""",
                (int(clock()) - 7 * 86400,),
            ).fetchall()
        ]
        return render_template("attempts.html", attempts=recent, now=int(clock()))

    @app.get("/allowlist")
    def allowlist_page():
        try:
            entries = backend.entries()
        except Exception as e:
            return render_template("error.html", message=f"Cannot read allowlist: {e}"), 502
        conn = db()
        pending_ops = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM outbox WHERE state != 'complete'"
            ).fetchall()
        ]
        audit = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM audit ORDER BY at DESC LIMIT 50"
            ).fetchall()
        ]
        return render_template(
            "allowlist.html", entries=entries, pending_ops=pending_ops, audit=audit
        )

    # ------------------------------------------------------------- actions

    @app.post("/api/approve")
    def api_approve():
        payload = request.get_json(force=True, silent=True) or {}
        sid = int(payload.get("submission_id", 0))
        sub, err = verified_sub_or_error(sid)
        if err:
            return err
        # Independent backend enforcement of the §7 rule — the disabled
        # button is UX, this is the check.
        if sub["status"] != "verified" or not sub.get("uuid") or not sub.get("attempt_event_id"):
            return (
                jsonify(
                    {
                        "error": "not_approvable",
                        "status": sub["status"],
                        "detail": "approval requires a verified submission with a stored UUID",
                    }
                ),
                409,
            )
        outbox = make_outbox()
        try:
            row = outbox.request(
                sid,
                "approved",
                uuid=sub["uuid"],
                reviewer=reviewer,
                notes=payload.get("notes"),
                raw_username=sub.get("raw_username"),
                platform=sub.get("platform"),
            )
        except OutboxBusy as e:
            return jsonify({"error": "decision_in_flight", "state": e.row["state"],
                            "message": honest_state(e.row)}), 409
        outbox.process(row)
        row = db().execute("SELECT * FROM outbox WHERE submission_id = ?", (sid,)).fetchone()
        return jsonify({"ok": True, "state": row["state"], "message": honest_state(row)})

    @app.post("/api/deny")
    def api_deny():
        payload = request.get_json(force=True, silent=True) or {}
        sid = int(payload.get("submission_id", 0))
        sub, err = verified_sub_or_error(sid)
        if err:
            return err
        if sub["status"] not in ("pending", "verified"):
            return jsonify({"error": "not_deniable", "status": sub["status"]}), 409
        outbox = make_outbox()
        try:
            row = outbox.request(
                sid,
                "denied",
                uuid=sub.get("uuid"),
                reviewer=reviewer,
                notes=payload.get("notes"),
                raw_username=sub.get("raw_username"),
                platform=sub.get("platform"),
            )
        except OutboxBusy as e:
            return jsonify({"error": "decision_in_flight", "state": e.row["state"],
                            "message": honest_state(e.row)}), 409
        outbox.process(row)
        row = db().execute("SELECT * FROM outbox WHERE submission_id = ?", (sid,)).fetchone()
        return jsonify({"ok": True, "state": row["state"], "message": honest_state(row)})

    @app.post("/api/retry")
    def api_retry():
        payload = request.get_json(force=True, silent=True) or {}
        sid = int(payload.get("submission_id", 0))
        row = db().execute("SELECT * FROM outbox WHERE submission_id = ?", (sid,)).fetchone()
        if row is None:
            return jsonify({"error": "no_outbox_row"}), 404
        make_outbox().process(row)
        row = db().execute("SELECT * FROM outbox WHERE submission_id = ?", (sid,)).fetchone()
        return jsonify({"ok": True, "state": row["state"], "message": honest_state(row)})

    @app.post("/api/abandon")
    def api_abandon():
        payload = request.get_json(force=True, silent=True) or {}
        sid = int(payload.get("submission_id", 0))
        try:
            make_outbox().abandon(sid, reviewer=reviewer, detail=str(payload.get("detail", "")))
        except OutboxError as e:
            return jsonify({"error": "abandon_failed", "detail": str(e)}), 409
        return jsonify({"ok": True, "message": "Abandoned; a fresh decision is now possible."})

    @app.post("/api/remove")
    def api_remove():
        payload = request.get_json(force=True, silent=True) or {}
        name = str(payload.get("name", ""))
        uuid = str(payload.get("uuid", ""))
        platform = str(payload.get("platform", "java"))
        if not uuid:
            return jsonify({"error": "uuid_required"}), 400
        try:
            backend.remove(name, uuid, platform)
            if uuid_present(backend.entries(), uuid):
                return jsonify({"error": "remove_not_confirmed",
                                "detail": "read-back still shows the UUID"}), 502
        except AllowlistConflict as e:
            return jsonify({"error": "conflict", "detail": str(e)}), 409
        except Exception as e:
            return jsonify({"error": "remove_failed", "detail": str(e)}), 502
        conn = db()
        with conn:
            conn.execute(
                """INSERT INTO audit (outbox_id, submission_id, action, uuid, reviewer, detail, at)
                   VALUES (NULL, NULL, 'manual_remove', ?, ?, ?, ?)""",
                (uuid, reviewer, f"manually removed {name!r}", int(clock())),
            )
        return jsonify({"ok": True, "message": f"Removed {name or uuid} (read-back confirmed)."})

    return app


# ---------------------------------------------------------------------- main


def build_backend(cfg):
    """Wire the §7 tier chosen in config. The §0 probe decides which tier is
    trustworthy; config records that decision."""
    kind = cfg.allowlist_backend
    if kind == "management":
        from .allowlist import ManagementBackend
        from .mgmt import ManagementClient

        secret = cfg.secret("MGMT_SECRET")
        if not secret:
            raise SystemExit("allowlist backend 'management' needs the MGMT_SECRET credential")
        return ManagementBackend(ManagementClient(cfg.management_url, secret))
    if kind == "rcon":
        from .allowlist import RconBackend
        from .rcon import Rcon

        password = cfg.secret("RCON_PASSWORD")
        if not password:
            raise SystemExit("allowlist backend 'rcon' needs the RCON_PASSWORD credential")
        return RconBackend(
            lambda: Rcon(cfg.rcon_host, cfg.rcon_port, password), cfg.whitelist_json
        )
    if kind == "file":
        from .allowlist import FileBackend

        reload_cmd = None
        if cfg.reload_via == "rcon":
            password = cfg.secret("RCON_PASSWORD")
            if not password:
                raise SystemExit("reload_via 'rcon' needs the RCON_PASSWORD credential")

            def reload_cmd():
                from .rcon import Rcon

                with Rcon(cfg.rcon_host, cfg.rcon_port, password) as rcon:
                    rcon.command("whitelist reload")

        elif cfg.reload_via == "management":
            from .mgmt import ManagementClient

            secret = cfg.secret("MGMT_SECRET")
            if not secret:
                raise SystemExit("reload_via 'management' needs the MGMT_SECRET credential")
            client = ManagementClient(cfg.management_url, secret)

            def reload_cmd():
                client.call("minecraft:allowlist/reload")

        return FileBackend(cfg.whitelist_json, reload_cmd=reload_cmd)
    raise SystemExit(f"unknown allowlist backend {kind!r}")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = load_config()
    admin_token = cfg.secret("ADMIN_API_TOKEN")
    if not admin_token:
        logger.critical("ADMIN_API_TOKEN credential missing (§9)")
        return 1
    if not cfg.admin_allowed_origins:
        logger.critical(
            "[admin] allowed_origins is empty; set it to the LAN origin nginx "
            "serves, e.g. [\"http://192.168.1.10\"]"
        )
        return 1

    worker = WorkerClient(cfg.worker_base_url, admin_token=admin_token)
    backend = build_backend(cfg)
    # §7 startup reconciliation before serving traffic.
    DecisionOutbox(
        connect(cfg.db_path), worker, backend, default_reviewer=cfg.reviewer
    ).reconcile_startup()

    def retry_loop():
        retry_conn = connect(cfg.db_path)
        retry_outbox = DecisionOutbox(retry_conn, worker, backend)
        while True:
            time.sleep(30)
            try:
                retry_outbox.process_all()
            except Exception:
                logger.exception("outbox retry loop iteration failed")

    threading.Thread(target=retry_loop, daemon=True, name="outbox-retry").start()

    app = create_app(
        worker=worker,
        backend=backend,
        db_path=cfg.db_path,
        allowed_origins=cfg.admin_allowed_origins,
        reviewer=cfg.reviewer,
    )

    from waitress import serve

    logger.info(
        "admin app on http://%s:%d (LAN access via nginx only)",
        cfg.admin_bind_host,
        cfg.admin_bind_port,
    )
    serve(app, host=cfg.admin_bind_host, port=cfg.admin_bind_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

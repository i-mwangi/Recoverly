from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from pathlib import Path
from uuid import uuid4

from src.local_console import service

blueprint = Blueprint(
    "local_console",
    __name__,
    url_prefix="/console",
    template_folder="templates",
    static_folder="static",
)


@blueprint.get("")
def console():
    sessions = service.list_sessions()
    selected_id = request.args.get("session", "")
    session = service.get_session(selected_id) if selected_id else None
    session = session or (service.get_session(sessions[0]["id"]) if sessions else service.create_session())
    return render_template("local_console/index.html", session=session, sessions=service.list_sessions(), agents=service.AGENTS)


@blueprint.post("/api/sessions")
def create_session():
    return jsonify(service.create_session()), 201


@blueprint.get("/api/sessions/<session_id>")
def get_session(session_id: str):
    session = service.get_session(session_id)
    if session is None:
        return jsonify({"error": "Session not found."}), 404
    return jsonify(session)


@blueprint.post("/api/sessions/<session_id>/messages")
def submit_message(session_id: str):
    try:
        payload = request.get_json(force=True)
        return jsonify(service.submit_message(session_id, str(payload.get("text") or "")))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@blueprint.post("/api/sessions/<session_id>/actions")
def run_action(session_id: str):
    try:
        payload = request.get_json(force=True)
        return jsonify(service.run_action(session_id, str(payload.get("message_id") or ""), str(payload.get("action_id") or "")))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@blueprint.post("/api/sessions/<session_id>/uploads")
def upload_document(session_id: str):
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "Choose a contract or invoice PDF."}), 400
    filename = Path(file.filename).name
    if Path(filename).suffix.lower() != ".pdf":
        return jsonify({"error": "Upload a PDF contract or invoice."}), 400
    target = Path("data") / "attachments" / f"{uuid4()}_{filename}"
    target.parent.mkdir(parents=True, exist_ok=True)
    file.save(target)
    try:
        return jsonify(service.ingest_document(session_id, target, filename))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

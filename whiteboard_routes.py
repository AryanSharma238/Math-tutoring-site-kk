"""Custom collaborative whiteboard: a real object-model canvas (Fabric.js on the client) with
its own persistence and sync layer, kept in its own module since it's a self-contained
subsystem within the tutoring site.

Architecture, in short:
- Each student has exactly one WhiteboardWorkspace, containing one or more WhiteboardPages.
- Each page holds any number of WhiteboardElements -- one row per stroke/text box/image/shape,
  never a single blob for the whole page. Creating, moving, or deleting one object only ever
  touches that one row.
- The client polls GET .../sync?since=<timestamp> every couple of seconds for whichever page is
  open, receiving only what changed since its last poll (new/updated elements + a small list of
  deleted IDs) -- not the whole page, and never on every mouse move. This is "near-real-time"
  (a second or two of latency), not a push-based websocket channel; see the README for why.
- Access control happens at the Flask route level (matching every other feature in this app --
  quizzes, curriculum, embeds all work the same way), not Postgres RLS: the backend connects to
  Postgres with one trusted role via SQLAlchemy, not through Supabase's PostgREST/RLS layer, so
  RLS policies would have nothing to attach to here. A student can only ever reach their own
  workspace_id; the admin can reach any.
"""

import json
import uuid
from datetime import datetime, timezone as dt_timezone

from flask import abort, jsonify, redirect, render_template, request, url_for

from models import (
    User, StudentProfile, WhiteboardWorkspace, WhiteboardPage, WhiteboardElement,
    WhiteboardDeletion, WhiteboardImage, db,
)

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB
ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp"}


def ensure_workspace(profile):
    """Module-level (not nested in register_whiteboard_routes) so other parts of the app --
    e.g. the dashboard route, which needs a workspace_id to embed the whiteboard tab -- can
    reuse it without needing their own copy of this logic."""
    ws = profile.whiteboard_workspace
    if ws is None:
        ws = WhiteboardWorkspace(profile_id=profile.id)
        db.session.add(ws)
        db.session.flush()  # get ws.id before creating the first page
        db.session.add(WhiteboardPage(workspace_id=ws.id, name="Page 1", position=0))
        db.session.commit()
    elif not ws.pages:
        # Shouldn't normally happen (a workspace is always created with one page), but stay
        # defensive -- a workspace with zero pages would leave the editor with nothing to show.
        db.session.add(WhiteboardPage(workspace_id=ws.id, name="Page 1", position=0))
        db.session.commit()
    return ws


def register_whiteboard_routes(app):
    from app import login_required, admin_required, current_user, get_supabase_storage

    _ensure_workspace = ensure_workspace

    def _authorize_workspace(workspace):
        user = current_user()
        if user.is_admin:
            return user
        if not user.profile or workspace.profile_id != user.profile.id:
            abort(403)
        return user

    def _get_workspace_or_404(workspace_id):
        ws = WhiteboardWorkspace.query.get_or_404(workspace_id)
        _authorize_workspace(ws)
        return ws

    def _get_page_or_404(page_id):
        page = WhiteboardPage.query.get_or_404(page_id)
        _authorize_workspace(page.workspace)
        return page

    def _get_element_or_404(element_id):
        element = WhiteboardElement.query.get_or_404(element_id)
        _authorize_workspace(element.page.workspace)
        return element

    def _iso_z(dt):
        """Format as UTC with a 'Z' suffix instead of '+00:00' -- the '+' in a raw offset
        gets silently decoded as a space if a query-string value is ever passed through
        without being percent-encoded (encodeURIComponent handles this correctly client-side,
        but avoiding the character entirely means a since= value can never silently fail to
        parse and fall back to a full resync just because something upstream forgot to encode
        it)."""
        return dt.replace(tzinfo=dt_timezone.utc).isoformat().replace("+00:00", "Z")

    def _page_json(page):
        return {"id": page.id, "name": page.name, "position": page.position}

    def _element_json(element):
        return {
            "id": element.id,
            "type": element.type,
            "data": json.loads(element.data),
            "updated_at": _iso_z(element.updated_at),
        }

    def _parse_since(raw):
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(dt_timezone.utc).replace(tzinfo=None)
        except ValueError:
            return None

    # ---------------- Page routes (render the editor) ----------------

    # A student's whiteboard is now embedded directly as a tab on their own dashboard
    # (student) or on the admin's per-student "Workspace" tab -- this standalone page just
    # stays around for whoever still has it bookmarked, redirecting into the right tab.
    @app.route("/whiteboard")
    @login_required
    def whiteboard():
        user = current_user()
        if user.is_admin:
            return redirect(url_for("dashboard"))
        return redirect(url_for("dashboard"))

    @app.route("/admin/whiteboards")
    @login_required
    @admin_required
    def admin_whiteboards():
        return redirect(url_for("dashboard"))

    @app.route("/admin/student/<int:user_id>/whiteboard")
    @login_required
    @admin_required
    def admin_student_whiteboard(user_id):
        return redirect(url_for("admin_student", user_id=user_id))

    # ---------------- Pages API ----------------

    @app.route("/api/whiteboard/<int:workspace_id>/pages", methods=["GET"])
    @login_required
    def api_list_pages(workspace_id):
        ws = _get_workspace_or_404(workspace_id)
        return jsonify({"pages": [_page_json(p) for p in ws.pages]})

    @app.route("/api/whiteboard/<int:workspace_id>/pages", methods=["POST"])
    @login_required
    def api_create_page(workspace_id):
        ws = _get_workspace_or_404(workspace_id)
        data = request.get_json(silent=True) or {}
        next_position = len(ws.pages)
        name = (data.get("name") or "").strip()[:120] or f"Page {next_position + 1}"
        page = WhiteboardPage(workspace_id=ws.id, name=name, position=next_position)
        db.session.add(page)
        db.session.commit()
        return jsonify(_page_json(page)), 201

    @app.route("/api/whiteboard/<int:workspace_id>/pages/<int:page_id>", methods=["PATCH"])
    @login_required
    def api_update_page(workspace_id, page_id):
        ws = _get_workspace_or_404(workspace_id)
        page = WhiteboardPage.query.filter_by(id=page_id, workspace_id=ws.id).first_or_404()
        data = request.get_json(silent=True) or {}

        if "name" in data:
            name = (data.get("name") or "").strip()[:120]
            if name:
                page.name = name

        if "position" in data:
            # Reorder: remove this page from the ordered list and reinsert at the requested
            # index, then renumber everyone -- simplest correct way to keep positions dense
            # and contiguous without a fiddly shuffle algorithm.
            try:
                target_index = max(0, min(int(data["position"]), len(ws.pages) - 1))
            except (TypeError, ValueError):
                target_index = None
            if target_index is not None:
                ordered = [p for p in ws.pages if p.id != page.id]
                ordered.insert(target_index, page)
                for i, p in enumerate(ordered):
                    p.position = i

        db.session.commit()
        return jsonify(_page_json(page))

    @app.route("/api/whiteboard/<int:workspace_id>/pages/<int:page_id>", methods=["DELETE"])
    @login_required
    def api_delete_page(workspace_id, page_id):
        ws = _get_workspace_or_404(workspace_id)
        page = WhiteboardPage.query.filter_by(id=page_id, workspace_id=ws.id).first_or_404()

        if len(ws.pages) <= 1:
            return jsonify({"error": "Can't delete the only remaining page."}), 400

        remaining = [p for p in ws.pages if p.id != page.id]
        db.session.delete(page)
        for i, p in enumerate(remaining):
            p.position = i
        db.session.commit()
        return jsonify({"pages": [_page_json(p) for p in remaining]})

    # ---------------- Elements API ----------------

    @app.route("/api/whiteboard/pages/<int:page_id>/sync", methods=["GET"])
    @login_required
    def api_sync_page(page_id):
        page = _get_page_or_404(page_id)
        since = _parse_since(request.args.get("since"))
        server_time = datetime.now(dt_timezone.utc)

        elements_q = WhiteboardElement.query.filter_by(page_id=page.id)
        if since is not None:
            elements_q = elements_q.filter(WhiteboardElement.updated_at > since)
        elements = elements_q.all()

        deleted_ids = []
        if since is not None:
            deletions = WhiteboardDeletion.query.filter(
                WhiteboardDeletion.page_id == page.id, WhiteboardDeletion.deleted_at > since,
            ).all()
            deleted_ids = [d.element_id for d in deletions]

        return jsonify({
            "elements": [_element_json(e) for e in elements],
            "deleted_ids": deleted_ids,
            "server_time": _iso_z(server_time),
            "full_sync": since is None,
        })

    @app.route("/api/whiteboard/pages/<int:page_id>/elements", methods=["POST"])
    @login_required
    def api_create_element(page_id):
        page = _get_page_or_404(page_id)
        data = request.get_json(silent=True) or {}
        el_type = (data.get("type") or "").strip()
        el_data = data.get("data")
        el_id = (data.get("id") or "").strip() or str(uuid.uuid4())

        if el_type not in ("path", "text", "image", "rect", "line", "circle", "arrow"):
            return jsonify({"error": "Unknown element type."}), 400
        if el_data is None:
            return jsonify({"error": "Missing element data."}), 400

        user = current_user()
        existing = WhiteboardElement.query.get(el_id)
        if existing and existing.page_id == page.id:
            existing.data = json.dumps(el_data)
            element = existing
        else:
            element = WhiteboardElement(
                id=el_id, page_id=page.id, type=el_type,
                data=json.dumps(el_data), created_by=user.id,
            )
            db.session.add(element)
        db.session.commit()
        return jsonify(_element_json(element)), 201

    @app.route("/api/whiteboard/elements/<string:element_id>", methods=["PATCH"])
    @login_required
    def api_update_element(element_id):
        element = _get_element_or_404(element_id)
        data = request.get_json(silent=True) or {}
        if "data" not in data:
            return jsonify({"error": "Missing data."}), 400
        element.data = json.dumps(data["data"])
        db.session.commit()
        return jsonify(_element_json(element))

    @app.route("/api/whiteboard/elements/<string:element_id>", methods=["DELETE"])
    @login_required
    def api_delete_element(element_id):
        element = _get_element_or_404(element_id)
        page_id = element.page_id
        db.session.delete(element)
        db.session.add(WhiteboardDeletion(page_id=page_id, element_id=element_id))
        db.session.commit()
        return jsonify({"ok": True})

    # ---------------- Image upload ----------------

    @app.route("/api/whiteboard/pages/<int:page_id>/upload-image", methods=["POST"])
    @login_required
    def api_upload_whiteboard_image(page_id):
        page = _get_page_or_404(page_id)
        file = request.files.get("image")
        if not file or not file.filename:
            return jsonify({"error": "No file uploaded."}), 400

        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ALLOWED_IMAGE_EXTS:
            return jsonify({"error": "Only PNG, JPG, JPEG, or WEBP images are allowed."}), 400

        raw = file.read()
        if len(raw) > MAX_IMAGE_BYTES:
            return jsonify({"error": "Image is too large (8MB max)."}), 400

        storage_path = f"page-{page.id}/{uuid.uuid4().hex}.{ext}"
        mimetype = file.mimetype or f"image/{ext}"

        # Try Supabase Storage first (the recommended, storage-efficient path -- see the
        # README). If it isn't configured, or the call fails for any reason (most commonly:
        # the "whiteboard-uploads" bucket hasn't been created yet), fall back to storing the
        # image locally so the upload still succeeds either way -- this feature shouldn't be
        # broken just because a separate manual setup step wasn't done first.
        try:
            client = get_supabase_storage()
            client.storage.from_("whiteboard-uploads").upload(
                storage_path, raw, {"content-type": mimetype},
            )
            url = client.storage.from_("whiteboard-uploads").get_public_url(storage_path)
            return jsonify({"url": url}), 201
        except Exception:
            pass

        image_id = str(uuid.uuid4())
        db.session.add(WhiteboardImage(id=image_id, page_id=page.id, mimetype=mimetype, data=raw))
        db.session.commit()
        return jsonify({"url": url_for("serve_whiteboard_image", image_id=image_id)}), 201

    @app.route("/whiteboard/images/<string:image_id>")
    @login_required
    def serve_whiteboard_image(image_id):
        from flask import Response
        image = WhiteboardImage.query.get_or_404(image_id)
        page = WhiteboardPage.query.get_or_404(image.page_id)
        _authorize_workspace(page.workspace)
        return Response(image.data, mimetype=image.mimetype)

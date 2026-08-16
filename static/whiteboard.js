// ============================================================================================
// Custom collaborative whiteboard editor.
//
// Every drawn stroke, text box, image, and shape is a real Fabric.js object (never raw
// pixels), so each one can be individually selected, moved, copied, and deleted. Persistence
// and sync happen through the Flask JSON API in whiteboard_routes.py: each object is one row
// server-side (WhiteboardElement), addressed by a client-generated UUID (`wbId`) that's stable
// from the moment it's created -- needed so undo/redo and the sync loop can always reference
// "this exact object" without waiting on a server round trip first.
//
// This module mounts into whatever `.wb-embed` container is on the page (see
// _whiteboard_editor.html) -- always inline in the page's own DOM, never an iframe pointing at
// a separate page, so opening it is instant (no navigation, no re-loading Fabric.js). Call
// WB.mount(rootEl) once per container the first time it actually needs to be visible; it's a
// no-op on a container that's already mounted.
//
// Sync model: listen for push events when available, and fall back to fast polling of
// GET /api/whiteboard/pages/<id>/sync?since=<last poll time> for anything missed. That keeps
// the board feeling live without depending on a single transport.
// ============================================================================================

window.WB = (function () {
  function mount(root) {
    if (!root || root.dataset.wbMounted === "true") return;
    root.dataset.wbMounted = "true";
    new WhiteboardEditor(root);
  }

  function WhiteboardEditor(root) {
    const workspaceId = root.dataset.workspaceId;
    if (!workspaceId) return;

    const q = (name) => root.querySelector(`[data-wb="${name}"]`);
    const canvasEl = q("canvas");
    const canvasWrap = q("canvasWrap");
    const statusEl = q("status");
    const headerPageEl = q("headerPage");
    const toolbar = q("toolbar");
    const colorSwatchesEl = q("colorSwatches");
    const colorCustomEl = q("colorCustom");
    const strokeWidthEl = q("strokeWidth");
    const pageTabsEl = q("pageTabs");
    const addPageBtn = q("addPage");
    const imageBtn = q("imageBtn");
    const imageInput = q("imageInput");
    const downloadBtn = q("downloadBtn");
    const maximizeBtn = q("maximizeBtn");
    const zoomInBtn = q("zoomInBtn");
    const zoomOutBtn = q("zoomOutBtn");
    const zoomLabel = q("zoomLabel");

    const canvas = new fabric.Canvas(canvasEl, {
      backgroundColor: "#ffffff",
      selection: true,
      preserveObjectStacking: true,
    });

    // ---------------- Sizing ----------------
    function resizeCanvas() {
      canvas.setWidth(canvasWrap.clientWidth);
      canvas.setHeight(canvasWrap.clientHeight);
      canvas.renderAll();
    }
    window.addEventListener("resize", resizeCanvas);
    // The container may still be display:none on first mount (e.g. a hidden tab panel) --
    // sizing to 0 would break the canvas permanently, so re-check on the next couple of
    // frames too, by which point the caller's tab-switch should have made it visible.
    requestAnimationFrame(resizeCanvas);
    setTimeout(resizeCanvas, 50);

    // ---------------- State ----------------
    let currentTool = "select";
    let currentColor = "#1a1a2e";
    let currentWidth = 5;
    let pages = [];
    let currentPageId = null;
    let lastSyncTime = null;
    let syncTimer = null;
    let suppressEvents = false; // true while applying remote changes, so we don't echo them back
    const recentlySent = new Map(); // elementId -> timestamp, to avoid jitter from our own echoes
    let eventSource = null;

    const undoStack = [];
    const redoStack = [];
    let clipboard = null;

    function setStatus(text, cls) {
      statusEl.textContent = text;
      statusEl.className = "wb-status" + (cls ? " " + cls : "");
    }

    function api(path, options) {
      return fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      }).then(async (resp) => {
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          throw new Error(body.error || `Request failed (${resp.status})`);
        }
        return resp.json();
      });
    }

    // ---------------- Tool switching ----------------
    function setTool(tool) {
      currentTool = tool;
      toolbar.querySelectorAll(".wb-tool[data-tool]").forEach((b) => {
        b.classList.toggle("active", b.dataset.tool === tool);
      });

      canvas.isDrawingMode = tool === "pen" || tool === "highlighter";
      canvas.selection = tool === "select";
      canvas.forEachObject((o) => { o.selectable = tool === "select"; });
      canvas.defaultCursor = tool === "eraser" ? "crosshair" : tool === "select" ? "default" : "crosshair";

      if (tool === "pen" || tool === "highlighter") {
        const brush = new fabric.PencilBrush(canvas);
        brush.width = currentWidth;
        brush.color = tool === "highlighter" ? hexToRgba(currentColor, 0.35) : currentColor;
        canvas.freeDrawingBrush = brush;
      }
      canvas.discardActiveObject();
      canvas.renderAll();
    }

    function hexToRgba(hex, alpha) {
      const r = parseInt(hex.slice(1, 3), 16);
      const g = parseInt(hex.slice(3, 5), 16);
      const b = parseInt(hex.slice(5, 7), 16);
      return `rgba(${r},${g},${b},${alpha})`;
    }

    toolbar.querySelectorAll(".wb-tool[data-tool]").forEach((btn) => {
      btn.addEventListener("click", () => setTool(btn.dataset.tool));
    });

    colorSwatchesEl.querySelectorAll(".wb-swatch[data-color]").forEach((btn) => {
      btn.addEventListener("click", () => {
        currentColor = btn.dataset.color;
        colorSwatchesEl.querySelectorAll(".wb-swatch").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        if (canvas.freeDrawingBrush) canvas.freeDrawingBrush.color = currentTool === "highlighter" ? hexToRgba(currentColor, 0.35) : currentColor;
      });
    });
    colorCustomEl.addEventListener("input", () => {
      currentColor = colorCustomEl.value;
      colorSwatchesEl.querySelectorAll(".wb-swatch").forEach((b) => b.classList.remove("active"));
      if (canvas.freeDrawingBrush) canvas.freeDrawingBrush.color = currentColor;
    });

    strokeWidthEl.addEventListener("input", () => {
      currentWidth = parseInt(strokeWidthEl.value, 10);
      if (canvas.freeDrawingBrush) canvas.freeDrawingBrush.width = currentWidth;
    });

    // ---------------- Zoom + pan (larger "world" than just the visible viewport) ----------------
    let zoom = 1;
    const ZOOM_MIN = 0.2;
    const ZOOM_MAX = 4;
    const PAN_BOUND = 3000; // how far the canvas can be panned from center, in canvas px

    function setZoom(newZoom, center) {
      zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, newZoom));
      const point = center || { x: canvas.getWidth() / 2, y: canvas.getHeight() / 2 };
      canvas.zoomToPoint(point, zoom);
      clampPan();
      zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
    }

    function clampPan() {
      const vpt = canvas.viewportTransform;
      vpt[4] = Math.max(-PAN_BOUND, Math.min(PAN_BOUND, vpt[4]));
      vpt[5] = Math.max(-PAN_BOUND, Math.min(PAN_BOUND, vpt[5]));
      canvas.requestRenderAll();
    }

    zoomInBtn.addEventListener("click", () => setZoom(zoom * 1.2));
    zoomOutBtn.addEventListener("click", () => setZoom(zoom / 1.2));

    canvas.on("mouse:wheel", (opt) => {
      // Trackpad two-finger scrolling pans. Pinch gestures (ctrl/meta on most
      // browsers) remain zoom, as do the dedicated zoom buttons.
      if (opt.e.ctrlKey || opt.e.metaKey) {
        const delta = opt.e.deltaY;
        setZoom(zoom * (delta > 0 ? 0.92 : 1.08), { x: opt.e.offsetX, y: opt.e.offsetY });
      } else {
        const vpt = canvas.viewportTransform;
        vpt[4] -= opt.e.deltaX || 0;
        vpt[5] -= opt.e.deltaY || 0;
        clampPan();
      }
      opt.e.preventDefault();
      opt.e.stopPropagation();
    });

    // Pan by holding Space and dragging, or middle-mouse-drag -- doesn't interfere with any
    // drawing tool since it only activates while the spacebar (or middle button) is held.
    let spaceHeld = false;
    let panning = false;
    let panStart = null;
    document.addEventListener("keydown", (e) => {
      if (e.code === "Space" && root.contains(document.activeElement) === false && document.activeElement !== document.body) return;
      if (e.code === "Space") { spaceHeld = true; canvas.defaultCursor = "grab"; }
    });
    document.addEventListener("keyup", (e) => {
      if (e.code === "Space") { spaceHeld = false; canvas.defaultCursor = currentTool === "select" ? "default" : "crosshair"; }
    });
    canvas.on("mouse:down", (opt) => {
      if (spaceHeld || opt.e.button === 1) {
        panning = true;
        panStart = { x: opt.e.clientX, y: opt.e.clientY };
        canvas.selection = false;
      }
    });
    canvas.on("mouse:move", (opt) => {
      if (!panning || !panStart) return;
      const vpt = canvas.viewportTransform;
      vpt[4] += opt.e.clientX - panStart.x;
      vpt[5] += opt.e.clientY - panStart.y;
      panStart = { x: opt.e.clientX, y: opt.e.clientY };
      clampPan();
    });
    canvas.on("mouse:up", () => {
      if (panning) {
        panning = false;
        canvas.selection = currentTool === "select";
      }
    });

    // Two-finger touch drag pans the board on tablets and phones. Register in the
    // capture phase so Fabric does not interpret the gesture as a drawing/select action.
    let touchPanning = false;
    let touchPanStart = null;
    const touchCanvas = canvas.upperCanvasEl;
    function touchMidpoint(touches) {
      return {
        x: (touches[0].clientX + touches[1].clientX) / 2,
        y: (touches[0].clientY + touches[1].clientY) / 2,
      };
    }
    touchCanvas.addEventListener("touchstart", (event) => {
      if (event.touches.length !== 2) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      touchPanning = true;
      panning = false;
      touchPanStart = touchMidpoint(event.touches);
      canvas.selection = false;
      canvas.defaultCursor = "grabbing";
    }, { passive: false, capture: true });
    touchCanvas.addEventListener("touchmove", (event) => {
      if (!touchPanning || event.touches.length < 2) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const point = touchMidpoint(event.touches);
      const vpt = canvas.viewportTransform;
      vpt[4] += point.x - touchPanStart.x;
      vpt[5] += point.y - touchPanStart.y;
      touchPanStart = point;
      clampPan();
    }, { passive: false, capture: true });
    function stopTouchPan(event) {
      if (!touchPanning) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      touchPanning = false;
      touchPanStart = null;
      canvas.selection = currentTool === "select";
      canvas.defaultCursor = currentTool === "select" ? "default" : "crosshair";
    }
    touchCanvas.addEventListener("touchend", stopTouchPan, { passive: false, capture: true });
    touchCanvas.addEventListener("touchcancel", stopTouchPan, { passive: false, capture: true });

    // ---------------- Shape / text creation ----------------
    let shapeStart = null;
    let activeShape = null;

    canvas.on("mouse:down", (opt) => {
      if (panning) return;
      if (["rect", "circle", "line", "arrow"].includes(currentTool)) {
        const p = canvas.getPointer(opt.e);
        shapeStart = p;
        activeShape = createShape(currentTool, p);
        if (activeShape) {
          activeShape.wbId = uuid();
          canvas.add(activeShape);
        }
      } else if (currentTool === "text") {
        const p = canvas.getPointer(opt.e);
        const text = new fabric.Textbox("Text", {
          left: p.x, top: p.y, fill: currentColor, fontSize: 20, width: 200,
        });
        text.wbId = uuid();
        canvas.add(text);
        canvas.setActiveObject(text);
        text.enterEditing();
        setTool("select");
      } else if (currentTool === "eraser") {
        eraseAt(opt.e);
      }
    });

    canvas.on("mouse:move", (opt) => {
      if (activeShape && shapeStart) {
        const p = canvas.getPointer(opt.e);
        updateShape(activeShape, currentTool, shapeStart, p);
        canvas.renderAll();
      } else if (currentTool === "eraser" && opt.e.buttons === 1) {
        eraseAt(opt.e);
      }
    });

    canvas.on("mouse:up", () => {
      if (activeShape) {
        pushUndo({ type: "add", element: serializeObject(activeShape) });
        persistCreate(activeShape);
      }
      activeShape = null;
      shapeStart = null;
    });

    function createShape(tool, p) {
      const opts = { left: p.x, top: p.y, stroke: currentColor, strokeWidth: currentWidth, fill: "transparent" };
      if (tool === "rect") return new fabric.Rect({ ...opts, width: 1, height: 1 });
      if (tool === "circle") return new fabric.Circle({ ...opts, radius: 1 });
      if (tool === "line") return new fabric.Line([p.x, p.y, p.x, p.y], { stroke: currentColor, strokeWidth: currentWidth });
      if (tool === "arrow") {
        const line = new fabric.Line([p.x, p.y, p.x, p.y], { stroke: currentColor, strokeWidth: currentWidth });
        line.wbArrow = true;
        return line;
      }
      return null;
    }

    function updateShape(shape, tool, start, p) {
      if (tool === "rect") {
        shape.set({
          width: Math.abs(p.x - start.x), height: Math.abs(p.y - start.y),
          left: Math.min(p.x, start.x), top: Math.min(p.y, start.y),
        });
      } else if (tool === "circle") {
        const r = Math.hypot(p.x - start.x, p.y - start.y) / 2;
        shape.set({ radius: r, left: Math.min(p.x, start.x), top: Math.min(p.y, start.y) });
      } else if (tool === "line" || tool === "arrow") {
        shape.set({ x2: p.x, y2: p.y });
      }
      shape.setCoords();
    }

    // ---------------- Eraser: deletes whole objects it touches, not pixels ----------------
    function eraseAt(evt) {
      const target = canvas.findTarget(evt, false);
      if (target && target.wbId) {
        pushUndo({ type: "remove", element: serializeObject(target) });
        canvas.remove(target);
        persistDelete(target.wbId);
      }
    }

    // ---------------- Freehand strokes (pen/highlighter) become their own objects ----------------
    canvas.on("path:created", (opt) => {
      const path = opt.path;
      path.wbId = uuid();
      pushUndo({ type: "add", element: serializeObject(path) });
      persistCreate(path);
    });

    // ---------------- Object move/resize -> persist ----------------
    canvas.on("object:modified", (opt) => {
      const obj = opt.target;
      if (!obj || !obj.wbId || suppressEvents) return;
      persistUpdate(obj);
    });

    // ---------------- Serialization ----------------
    function serializeObject(obj) {
      return { id: obj.wbId, type: wbTypeFor(obj), json: obj.toObject(["wbArrow"]) };
    }

    function wbTypeFor(obj) {
      if (obj.wbArrow) return "arrow";
      if (obj.type === "path") return "path";
      if (obj.type === "textbox" || obj.type === "text") return "text";
      if (obj.type === "image") return "image";
      if (obj.type === "rect") return "rect";
      if (obj.type === "circle") return "circle";
      if (obj.type === "line") return "line";
      return obj.type;
    }

    function fabricClassFor(type) {
      return { path: fabric.Path, text: fabric.Textbox, image: fabric.Image, rect: fabric.Rect, circle: fabric.Circle, line: fabric.Line, arrow: fabric.Line }[type];
    }

    // ---------------- Persistence (create/update/delete one element) ----------------
    function markSaved() {
      setStatus("Saved", "wb-status-saved");
      refreshCurrentThumbnail();
    }

    function persistCreate(obj) {
      const payload = serializeObject(obj);
      recentlySent.set(payload.id, Date.now());
      setStatus("Saving...", "wb-status-saving");
      api(`/api/whiteboard/pages/${currentPageId}/elements`, {
        method: "POST",
        body: JSON.stringify({ id: payload.id, type: payload.type, data: payload.json }),
      }).then(markSaved).catch(onSaveError);
    }

    function persistUpdate(obj) {
      if (!obj.wbId) return;
      const payload = serializeObject(obj);
      recentlySent.set(obj.wbId, Date.now());
      setStatus("Saving...", "wb-status-saving");
      api(`/api/whiteboard/elements/${obj.wbId}`, {
        method: "PATCH",
        body: JSON.stringify({ data: payload.json }),
      }).then(markSaved).catch(onSaveError);
    }

    function persistDelete(id) {
      recentlySent.set(id, Date.now());
      setStatus("Saving...", "wb-status-saving");
      api(`/api/whiteboard/elements/${id}`, { method: "DELETE" })
        .then(markSaved)
        .catch(onSaveError);
    }

    function onSaveError(err) {
      // Never clear local state on a failed save -- keep what the user drew, just flag it.
      setStatus("Couldn't save -- retrying...", "wb-status-error");
      console.error(err);
    }

    // ---------------- Undo / redo (element-level, not full-canvas snapshots) ----------------
    function pushUndo(op) {
      if (suppressEvents) return;
      undoStack.push(op);
      redoStack.length = 0;
    }

    function findByWbId(id) {
      return canvas.getObjects().find((o) => o.wbId === id);
    }

    function undo() {
      const op = undoStack.pop();
      if (!op) return;
      suppressEvents = true;
      if (op.type === "add") {
        const obj = findByWbId(op.element.id);
        if (obj) canvas.remove(obj);
        persistDelete(op.element.id);
      } else if (op.type === "remove") {
        restoreElement(op.element).then((obj) => persistCreate(obj));
      } else if (op.type === "modify") {
        const obj = findByWbId(op.element.id);
        if (obj) {
          obj.set(op.before.json);
          obj.setCoords();
          canvas.renderAll();
          persistUpdate(obj);
        }
      }
      redoStack.push(op);
      suppressEvents = false;
    }

    function redo() {
      const op = redoStack.pop();
      if (!op) return;
      suppressEvents = true;
      if (op.type === "add") {
        restoreElement(op.element).then((obj) => persistCreate(obj));
      } else if (op.type === "remove") {
        const obj = findByWbId(op.element.id);
        if (obj) canvas.remove(obj);
        persistDelete(op.element.id);
      } else if (op.type === "modify") {
        const obj = findByWbId(op.element.id);
        if (obj) {
          obj.set(op.after.json);
          obj.setCoords();
          canvas.renderAll();
          persistUpdate(obj);
        }
      }
      suppressEvents = false;
    }

    function restoreElement(element) {
      return addElementToCanvas(element.id, element.type, element.json);
    }

    // ---------------- Copy / cut / paste ----------------
    // Deep-clones the current selection (one or several objects) into `clipboard` as an array
    // of {type, json} pairs, deliberately never carrying the original wbId along -- pasting
    // always mints brand-new ids so a paste can never collide with (or silently overwrite) the
    // object it was copied from.
    function copySelection() {
      const active = canvas.getActiveObject();
      if (!active) return;
      const objects = active.type === "activeSelection" ? active.getObjects() : [active];
      clipboard = objects.map((o) => ({ type: wbTypeFor(o), json: o.toObject(["wbArrow"]) }));
    }

    function pasteClipboard() {
      if (!clipboard || !clipboard.length) return;
      canvas.discardActiveObject();
      const toSelect = [];
      // Paste every copied object together as one operation (Promise.all), then select them
      // all as a group -- avoids the previous per-item .then() chains racing each other and
      // leaving a paste half-applied.
      Promise.all(
        clipboard.map(({ type, json }) => {
          const clone = { ...json, left: (json.left || 0) + 20, top: (json.top || 0) + 20 };
          delete clone.wbId;
          const newId = uuid();
          return addElementToCanvas(newId, type, clone).then((obj) => {
            if (!obj) return null;
            pushUndo({ type: "add", element: serializeObject(obj) });
            persistCreate(obj);
            toSelect.push(obj);
            return obj;
          });
        })
      ).then(() => {
        if (toSelect.length === 1) {
          canvas.setActiveObject(toSelect[0]);
        } else if (toSelect.length > 1) {
          const sel = new fabric.ActiveSelection(toSelect, { canvas });
          canvas.setActiveObject(sel);
        }
        canvas.requestRenderAll();
      });
    }

    function cutSelection() {
      const active = canvas.getActiveObject();
      if (!active) return;
      copySelection();
      const objs = active.type === "activeSelection" ? active.getObjects() : [active];
      objs.forEach((o) => {
        if (o.wbId) {
          pushUndo({ type: "remove", element: serializeObject(o) });
          persistDelete(o.wbId);
        }
        canvas.remove(o);
      });
      canvas.discardActiveObject();
      canvas.renderAll();
    }

    function deleteSelection() {
      const active = canvas.getActiveObject();
      if (!active) return;
      const objs = active.type === "activeSelection" ? active.getObjects() : [active];
      objs.forEach((o) => {
        if (o.wbId) {
          pushUndo({ type: "remove", element: serializeObject(o) });
          persistDelete(o.wbId);
        }
        canvas.remove(o);
      });
      canvas.discardActiveObject();
      canvas.renderAll();
    }

    function isEventInsideThisEditor(e) {
      // Multiple whiteboard editors can exist on the same page (e.g. a hidden standalone one
      // plus an embedded one); only handle keyboard shortcuts when this specific editor's
      // canvas actually has focus/hover, so Ctrl+C in one doesn't leak into another.
      return root.contains(document.activeElement) || root.matches(":hover");
    }

    document.addEventListener("keydown", (e) => {
      if (!isEventInsideThisEditor(e) && document.activeElement === document.body) {
        // Fall through: if nothing else has focus, still allow shortcuts for the only visible
        // editor (covers the common single-board-on-page case).
      }
      const active = canvas.getActiveObject();
      const isEditingText = active && active.isEditing;
      if (isEditingText) return; // let normal text-editing keys through
      if (!root.offsetParent) return; // this editor isn't visible right now

      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === "z" && e.shiftKey) { e.preventDefault(); redo(); }
      else if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); }
      else if (mod && e.key.toLowerCase() === "c") { e.preventDefault(); copySelection(); }
      else if (mod && e.key.toLowerCase() === "x") { e.preventDefault(); cutSelection(); }
      else if (mod && e.key.toLowerCase() === "v") { e.preventDefault(); pasteClipboard(); }
      else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSelection(); }
      else if (e.key.toLowerCase() === "v" && !mod) setTool("select");
      else if (e.key.toLowerCase() === "p" && !mod) setTool("pen");
      else if (e.key.toLowerCase() === "h" && !mod) setTool("highlighter");
      else if (e.key.toLowerCase() === "e" && !mod) setTool("eraser");
      else if (e.key.toLowerCase() === "t" && !mod) setTool("text");
    });

    // Track modification start (for undo "before" state) -- captured on selection so a drag/
    // resize's undo entry knows what to revert to.
    let modifyBefore = null;
    canvas.on("object:moving", (opt) => { if (!modifyBefore) modifyBefore = serializeObject(opt.target); });
    canvas.on("object:scaling", (opt) => { if (!modifyBefore) modifyBefore = serializeObject(opt.target); });
    canvas.on("object:modified", (opt) => {
      if (modifyBefore && opt.target && opt.target.wbId === modifyBefore.id) {
        pushUndo({ type: "modify", element: { id: modifyBefore.id }, before: modifyBefore, after: serializeObject(opt.target) });
      }
      modifyBefore = null;
    });

    // ---------------- Image upload ----------------
    imageBtn.addEventListener("click", () => imageInput.click());
    imageInput.addEventListener("change", () => {
      const file = imageInput.files[0];
      if (!file) return;
      const formData = new FormData();
      formData.append("image", file);
      setStatus("Uploading image...", "wb-status-saving");
      fetch(`/api/whiteboard/pages/${currentPageId}/upload-image`, { method: "POST", body: formData })
        .then(async (resp) => {
          const body = await resp.json();
          if (!resp.ok) throw new Error(body.error || "Upload failed.");
          return body;
        })
        .then(({ url }) => {
          fabric.Image.fromURL(url, (img) => {
            if (!img || !img.width) {
              setStatus("Image failed to load after upload.", "wb-status-error");
              return;
            }
            img.scaleToWidth(Math.min(400, canvas.getWidth() * 0.5));
            img.set({ left: 60, top: 60 });
            img.wbId = uuid();
            canvas.add(img);
            pushUndo({ type: "add", element: serializeObject(img) });
            persistCreate(img);
            setStatus("Saved", "wb-status-saved");
          }, { crossOrigin: "anonymous" });
        })
        .catch((err) => { setStatus(err.message, "wb-status-error"); });
      imageInput.value = "";
    });

    // ---------------- Adding a server element to the canvas (used by initial load + sync + undo) ----------------
    function addElementToCanvas(id, type, json) {
      return new Promise((resolve) => {
        if (type === "image") {
          fabric.Image.fromObject(json, (img) => {
            img.wbId = id;
            canvas.add(img);
            resolve(img);
          });
          return;
        }
        const Cls = fabricClassFor(type);
        if (!Cls) { resolve(null); return; }
        if (type === "path") {
          fabric.util.enlivenObjects([json], (objects) => {
            const obj = objects[0];
            obj.wbId = id;
            canvas.add(obj);
            resolve(obj);
          });
          return;
        }
        const obj = new Cls(json.text || "", json);
        Object.assign(obj, json);
        obj.wbId = id;
        if (type === "arrow") obj.wbArrow = true;
        canvas.add(obj);
        resolve(obj);
      });
    }

    // ---------------- Page management ----------------
    // Each tab shows a small live thumbnail of that page's content instead of just a number,
    // so switching pages is recognizable at a glance (like a slide sorter).
    const pageThumbnails = new Map(); // pageId -> dataURL

    function renderPageTabs() {
      pageTabsEl.innerHTML = "";
      pages.forEach((p) => {
        const tab = document.createElement("div");
        tab.className = "wb-page-tab" + (p.id === currentPageId ? " active" : "");
        tab.dataset.pageId = p.id;

        const thumb = document.createElement("div");
        thumb.className = "wb-page-thumb";
        const cachedThumb = pageThumbnails.get(p.id);
        if (cachedThumb) thumb.style.backgroundImage = `url(${cachedThumb})`;
        tab.appendChild(thumb);

        const label = document.createElement("span");
        label.className = "wb-page-tab-label";
        label.textContent = p.name;
        label.addEventListener("dblclick", (e) => {
          e.stopPropagation();
          renamePage(p.id);
        });
        tab.appendChild(label);

        if (pages.length > 1) {
          const closeBtn = document.createElement("button");
          closeBtn.type = "button";
          closeBtn.className = "wb-page-tab-close";
          closeBtn.textContent = "×";
          closeBtn.title = "Delete page";
          closeBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            deletePage(p.id);
          });
          tab.appendChild(closeBtn);
        }

        tab.addEventListener("click", () => switchToPage(p.id));
        pageTabsEl.appendChild(tab);

        if (!pageThumbnails.has(p.id)) refreshThumbnailFor(p.id);
      });
      const current = pages.find((p) => p.id === currentPageId);
      headerPageEl.textContent = current ? current.name : "";
    }

    function updateThumbnailTabImage(pageId, dataUrl) {
      pageThumbnails.set(pageId, dataUrl);
      const tab = pageTabsEl.querySelector(`.wb-page-tab[data-page-id="${pageId}"] .wb-page-thumb`);
      if (tab) tab.style.backgroundImage = `url(${dataUrl})`;
    }

    // Renders the CURRENTLY OPEN page's thumbnail directly from the live canvas -- instant,
    // no extra network round trip.
    function refreshCurrentThumbnail() {
      if (!currentPageId) return;
      try {
        const dataUrl = canvas.toDataURL({ format: "png", multiplier: 0.15 });
        updateThumbnailTabImage(currentPageId, dataUrl);
      } catch {
        // toDataURL can fail on a tainted canvas (e.g. a cross-origin image without CORS
        // headers) -- thumbnails are a nice-to-have, never worth breaking the board over.
      }
    }

    // Renders a thumbnail for a page that ISN'T currently open, by fetching its elements and
    // drawing them on a small offscreen canvas -- used the first time a tab appears, and
    // periodically refreshed so other collaborators' edits show up here too (see pollPages).
    function refreshThumbnailFor(pageId) {
      api(`/api/whiteboard/pages/${pageId}/sync`).then((data) => {
        const off = new fabric.StaticCanvas(null, { width: 160, height: 100, backgroundColor: "#ffffff" });
        Promise.all(data.elements.map((el) => addElementToOffscreenCanvas(off, el.type, el.data))).then(() => {
          off.renderAll();
          updateThumbnailTabImage(pageId, off.toDataURL({ format: "png" }));
          off.dispose();
        });
      }).catch(() => {});
    }

    function loadPages() {
      return api(`/api/whiteboard/${workspaceId}/pages`).then(({ pages: list }) => {
        pages = list;
        if (!currentPageId || !pages.find((p) => p.id === currentPageId)) {
          currentPageId = pages[0].id;
        }
        renderPageTabs();
      });
    }

    // Poll the page LIST itself (not just the open page's elements) every few seconds, so a
    // page added/renamed/deleted by the other collaborator shows up here too -- without this,
    // only element-level changes on the page you already have open would ever sync.
    let pagesPollInFlight = false;
    function pollPages() {
      if (pagesPollInFlight) return;
      pagesPollInFlight = true;
      api(`/api/whiteboard/${workspaceId}/pages`).then(({ pages: list }) => {
        const changed = JSON.stringify(list) !== JSON.stringify(pages);
        if (!changed) return;
        const stillExists = list.some((p) => p.id === currentPageId);
        pages = list;
        if (!stillExists) {
          // The page we had open was deleted by someone else -- fall back to the first page.
          currentPageId = null;
          switchToPage(pages[0].id);
        } else {
          renderPageTabs();
        }
      }).catch(() => {}).finally(() => { pagesPollInFlight = false; });
    }

    function switchToPage(pageId) {
      if (pageId === currentPageId) return;
      stopSync();
      currentPageId = pageId;
      lastSyncTime = null;
      canvas.clear();
      canvas.backgroundColor = "#ffffff";
      renderPageTabs();
      fullLoadCurrentPage().then(startSync);
    }

    function fullLoadCurrentPage() {
      setStatus("Loading...", "wb-status-saving");
      return api(`/api/whiteboard/pages/${currentPageId}/sync`).then((data) => {
        const loads = data.elements.map((el) => addElementToCanvas(el.id, el.type, el.data));
        return Promise.all(loads).then(() => {
          canvas.renderAll();
          lastSyncTime = data.server_time;
          setStatus("Saved", "wb-status-saved");
          refreshCurrentThumbnail();
        });
      });
    }

    addPageBtn.addEventListener("click", () => {
      api(`/api/whiteboard/${workspaceId}/pages`, { method: "POST", body: JSON.stringify({}) })
        .then((page) => {
          pages.push(page);
          switchToPage(page.id);
        });
    });

    function renamePage(pageId) {
      const page = pages.find((p) => p.id === pageId);
      if (!page) return;
      const name = prompt("Page name", page.name);
      if (!name || !name.trim()) return;
      api(`/api/whiteboard/${workspaceId}/pages/${pageId}`, {
        method: "PATCH", body: JSON.stringify({ name: name.trim() }),
      }).then((updated) => {
        page.name = updated.name;
        renderPageTabs();
      });
    }

    function deletePage(pageId) {
      if (pages.length <= 1) return; // one page must always remain
      if (!confirm("Delete this page? This can't be undone.")) return;
      api(`/api/whiteboard/${workspaceId}/pages/${pageId}`, { method: "DELETE" })
        .then(({ pages: remaining }) => {
          pages = remaining;
          pageThumbnails.delete(pageId);
          if (currentPageId === pageId) {
            currentPageId = null;
            switchToPage(pages[0].id);
          } else {
            renderPageTabs();
          }
        })
        .catch((err) => alert(err.message));
    }

    // ---------------- Sync polling ----------------
    let pagesSyncTimer = null;

    function startSync() {
      stopSync();
      // Render's default Gunicorn worker is synchronous. Do not open a long-lived
      // EventSource connection here: it can occupy the only worker and make the
      // entire dashboard look frozen on reload. Short, non-overlapping requests
      // keep updates responsive without blocking normal page loads.
      syncTimer = setInterval(pollSync, 750);
      pagesSyncTimer = setInterval(pollPages, 2500);
    }

    function stopSync() {
      if (syncTimer) clearInterval(syncTimer);
      if (pagesSyncTimer) clearInterval(pagesSyncTimer);
      if (eventSource) eventSource.close();
      syncTimer = null;
      pagesSyncTimer = null;
      eventSource = null;
    }

    function startEventStream() {
      if (!currentPageId || typeof EventSource === "undefined") return;
      try {
        eventSource = new EventSource(`/api/whiteboard/pages/${currentPageId}/events`);
        eventSource.addEventListener("change", () => {
          pollSync();
        });
        eventSource.onerror = () => {
          if (eventSource) {
            eventSource.close();
            eventSource = null;
          }
        };
      } catch {
        eventSource = null;
      }
    }

    let syncInFlight = false;
    function pollSync() {
      if (!currentPageId) return;
      if (syncInFlight) return;
      syncInFlight = true;
      const url = `/api/whiteboard/pages/${currentPageId}/sync?since=${encodeURIComponent(lastSyncTime)}`;
      api(url)
        .then((data) => {
          const now = Date.now();
          const incoming = data.elements.filter((el) => {
            const sentAt = recentlySent.get(el.id);
            return !sentAt || now - sentAt > 2000;
          });

          suppressEvents = true;
          incoming.forEach((el) => {
            const existing = findByWbId(el.id);
            if (existing) {
              existing.set(el.data);
              existing.setCoords();
            } else {
              addElementToCanvas(el.id, el.type, el.data);
            }
          });
          data.deleted_ids.forEach((id) => {
            const existing = findByWbId(id);
            if (existing) canvas.remove(existing);
          });
          canvas.renderAll();
          suppressEvents = false;

          lastSyncTime = data.server_time;
          setStatus("Saved", "wb-status-saved");
          if (incoming.length || data.deleted_ids.length) refreshCurrentThumbnail();
        })
        .catch(() => {
          setStatus("Reconnecting...", "wb-status-error");
        }).finally(() => { syncInFlight = false; });
    }

    // ---------------- PDF export (client-side, every page) ----------------
    downloadBtn.addEventListener("click", async () => {
      downloadBtn.disabled = true;
      downloadBtn.textContent = "Exporting...";
      try {
        const { jsPDF } = window.jspdf;
        const pdf = new jsPDF({ orientation: "landscape", unit: "px", format: [canvas.getWidth(), canvas.getHeight()] });
        const exportScale = 2.5;

        for (let i = 0; i < pages.length; i++) {
          const page = pages[i];
          const data = await api(`/api/whiteboard/pages/${page.id}/sync`);
          const offCanvas = new fabric.StaticCanvas(null, {
            width: canvas.getWidth() * exportScale,
            height: canvas.getHeight() * exportScale,
            backgroundColor: "#ffffff",
          });
          await Promise.all(data.elements.map((el) => addElementToOffscreenCanvas(offCanvas, el.type, el.data)));
          offCanvas.setZoom(exportScale);
          offCanvas.renderAll();
          const imgData = offCanvas.toDataURL({ format: "png", multiplier: 1 });
          if (i > 0) pdf.addPage([canvas.getWidth(), canvas.getHeight()], "landscape");
          pdf.addImage(imgData, "PNG", 0, 0, canvas.getWidth(), canvas.getHeight());
          offCanvas.dispose();
        }

        pdf.save("whiteboard.pdf");
      } catch (err) {
        alert("Couldn't export PDF: " + err.message);
      } finally {
        downloadBtn.disabled = false;
        downloadBtn.textContent = "Download PDF";
      }
    });

    function addElementToOffscreenCanvas(offCanvas, type, json) {
      return new Promise((resolve) => {
        if (type === "image") {
          fabric.Image.fromObject(json, (img) => { offCanvas.add(img); resolve(); });
          return;
        }
        if (type === "path") {
          fabric.util.enlivenObjects([json], (objects) => { offCanvas.add(objects[0]); resolve(); });
          return;
        }
        const Cls = fabricClassFor(type);
        if (!Cls) { resolve(); return; }
        const obj = new Cls(json.text || "", json);
        Object.assign(obj, json);
        offCanvas.add(obj);
        resolve();
      });
    }

    // ---------------- Maximize (CSS fullscreen toggle of this same DOM element -- no reload) ----------------
    if (maximizeBtn) {
      maximizeBtn.addEventListener("click", () => {
        root.classList.toggle("wb-embed-maximized");
        requestAnimationFrame(resizeCanvas);
      });
    }

    // ---------------- Utility ----------------
    function uuid() {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
      return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0, v = c === "x" ? r : (r & 0x3) | 0x8;
        return v.toString(16);
      });
    }

    // ---------------- Boot ----------------
    setTool("select");
    loadPages().then(() => fullLoadCurrentPage()).then(startSync);
  }

  return { mount };
})();

// Auto-mount any whiteboard editor that's visible on page load (the standalone /whiteboard
// page). Editors embedded in a hidden tab mount lazily when that tab is first shown instead
// (see the tab-switching JS in student_dashboard.html / admin_student.html).
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".wb-embed[data-workspace-id]").forEach((el) => {
    if (el.offsetParent !== null) WB.mount(el);
  });
});

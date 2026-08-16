// ============================================================================================
// Custom collaborative whiteboard editor.
//
// Every drawn stroke, text box, image, and shape is a real Fabric.js object (never raw
// pixels), so each one can be individually selected, moved, copied, and deleted. Persistence
// and sync happen through the Flask JSON API in whiteboard_routes.py: each object is one row
// server-side (WhiteboardElement), addressed by a client-generated UUID (`wbId`) that's stable
// from the moment the object is created -- needed so undo/redo and the sync loop can always
// reference "this exact object" without waiting on a server round trip first.
//
// Sync model: every ~1.5s, poll GET /api/whiteboard/pages/<id>/sync?since=<last poll time> for
// whatever changed on the current page since the last poll, and apply just those changes to
// the local canvas. This is "near-real-time" (a second or two of latency), not a push-based
// websocket -- see the README for why, given this app's existing architecture.
// ============================================================================================

(function () {
  const workspaceId = window.WB_WORKSPACE_ID;
  if (!workspaceId) return;

  const canvasEl = document.getElementById("wbCanvas");
  const canvasWrap = document.getElementById("wbCanvasWrap");
  const statusEl = document.getElementById("wbStatus");
  const headerPageEl = document.getElementById("wbHeaderPage");
  const toolbar = document.getElementById("wbToolbar");
  const colorSwatchesEl = document.getElementById("wbColorSwatches");
  const colorCustomEl = document.getElementById("wbColorCustom");
  const strokeWidthEl = document.getElementById("wbStrokeWidth");
  const pageTabsEl = document.getElementById("wbPageTabs");
  const addPageBtn = document.getElementById("wbAddPage");
  const imageBtn = document.getElementById("wbImageBtn");
  const imageInput = document.getElementById("wbImageInput");
  const downloadBtn = document.getElementById("wbDownloadBtn");

  const canvas = new fabric.Canvas("wbCanvas", {
    backgroundColor: "#fdfcf7",
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
      applyColorToSelection();
    });
  });
  colorCustomEl.addEventListener("input", () => {
    currentColor = colorCustomEl.value;
    colorSwatchesEl.querySelectorAll(".wb-swatch").forEach((b) => b.classList.remove("active"));
    if (canvas.freeDrawingBrush) canvas.freeDrawingBrush.color = currentColor;
    applyColorToSelection();
  });

  strokeWidthEl.addEventListener("change", () => {
    currentWidth = parseInt(strokeWidthEl.value, 10);
    if (canvas.freeDrawingBrush) canvas.freeDrawingBrush.width = currentWidth;
  });

  function applyColorToSelection() {
    // Changing the color swatch only affects FUTURE strokes/shapes -- it does not repaint
    // whatever's currently selected. (An explicit "recolor selection" affordance could be
    // added later; keeping this simple matches the "changing color must not affect
    // previously-created strokes" requirement unambiguously.)
  }

  // ---------------- Shape / text creation ----------------
  let shapeStart = null;
  let activeShape = null;

  canvas.on("mouse:down", (opt) => {
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
    return { id: obj.wbId, type: wbTypeFor(obj), json: obj.toObject(["wbId", "wbArrow"]) };
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
  function persistCreate(obj) {
    const payload = serializeObject(obj);
    recentlySent.set(payload.id, Date.now());
    setStatus("Saving...", "wb-status-saving");
    api(`/api/whiteboard/pages/${currentPageId}/elements`, {
      method: "POST",
      body: JSON.stringify({ id: payload.id, type: payload.type, data: payload.json }),
    }).then(() => setStatus("Saved", "wb-status-saved")).catch(onSaveError);
  }

  function persistUpdate(obj) {
    if (!obj.wbId) return;
    const payload = serializeObject(obj);
    recentlySent.set(obj.wbId, Date.now());
    setStatus("Saving...", "wb-status-saving");
    api(`/api/whiteboard/elements/${obj.wbId}`, {
      method: "PATCH",
      body: JSON.stringify({ data: payload.json }),
    }).then(() => setStatus("Saved", "wb-status-saved")).catch(onSaveError);
  }

  function persistDelete(id) {
    recentlySent.set(id, Date.now());
    setStatus("Saving...", "wb-status-saving");
    api(`/api/whiteboard/elements/${id}`, { method: "DELETE" })
      .then(() => setStatus("Saved", "wb-status-saved"))
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
  function copySelection() {
    const active = canvas.getActiveObject();
    if (!active) return;
    clipboard = active.type === "activeSelection"
      ? active.getObjects().map((o) => o.toObject(["wbId", "wbArrow"]))
      : [active.toObject(["wbId", "wbArrow"])];
  }

  function pasteClipboard() {
    if (!clipboard) return;
    clipboard.forEach((json) => {
      const clone = { ...json, left: (json.left || 0) + 20, top: (json.top || 0) + 20 };
      const newId = uuid();
      addElementToCanvas(newId, wbTypeFromJsonType(json.type), clone).then((obj) => {
        pushUndo({ type: "add", element: serializeObject(obj) });
        persistCreate(obj);
      });
    });
  }

  function wbTypeFromJsonType(fabricType) {
    return { path: "path", textbox: "text", text: "text", image: "image", rect: "rect", circle: "circle", line: "line" }[fabricType] || fabricType;
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

  document.addEventListener("keydown", (e) => {
    const active = canvas.getActiveObject();
    const isEditingText = active && active.isEditing;
    if (isEditingText) return; // let normal text-editing keys through

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
  function renderPageTabs() {
    pageTabsEl.innerHTML = "";
    pages.forEach((p) => {
      const tab = document.createElement("div");
      tab.className = "wb-page-tab" + (p.id === currentPageId ? " active" : "");
      tab.dataset.pageId = p.id;

      const label = document.createElement("span");
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
    });
    const current = pages.find((p) => p.id === currentPageId);
    headerPageEl.textContent = current ? current.name : "";
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

  function switchToPage(pageId) {
    if (pageId === currentPageId) return;
    stopSync();
    currentPageId = pageId;
    lastSyncTime = null;
    canvas.clear();
    canvas.backgroundColor = "#fdfcf7";
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
  function startSync() {
    stopSync();
    syncTimer = setInterval(pollSync, 1500);
  }

  function stopSync() {
    if (syncTimer) clearInterval(syncTimer);
    syncTimer = null;
  }

  function pollSync() {
    if (!currentPageId) return;
    const url = `/api/whiteboard/pages/${currentPageId}/sync?since=${encodeURIComponent(lastSyncTime)}`;
    api(url)
      .then((data) => {
        const now = Date.now();
        // Skip applying elements we ourselves just sent -- avoids visual jitter from an echo
        // of our own change coming back through the poll before it's worth reconciling.
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
      })
      .catch(() => {
        setStatus("Reconnecting...", "wb-status-error");
        // Local state is untouched -- next successful poll just picks up where we left off.
      });
  }

  // ---------------- PDF export (client-side, every page) ----------------
  downloadBtn.addEventListener("click", async () => {
    downloadBtn.disabled = true;
    downloadBtn.textContent = "Exporting...";
    try {
      const { jsPDF } = window.jspdf;
      const pdf = new jsPDF({ orientation: "landscape", unit: "px", format: [canvas.getWidth(), canvas.getHeight()] });

      for (let i = 0; i < pages.length; i++) {
        const page = pages[i];
        const data = await api(`/api/whiteboard/pages/${page.id}/sync`);
        const offCanvas = new fabric.StaticCanvas(null, {
          width: canvas.getWidth(), height: canvas.getHeight(), backgroundColor: "#fdfcf7",
        });
        await Promise.all(data.elements.map((el) => addElementToOffscreenCanvas(offCanvas, el.type, el.data)));
        offCanvas.renderAll();
        const imgData = offCanvas.toDataURL({ format: "png" });
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

  // ---------------- Utility ----------------
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0, v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  // ---------------- Boot ----------------
  resizeCanvas();
  setTool("select");
  loadPages().then(() => fullLoadCurrentPage()).then(startSync);
})();

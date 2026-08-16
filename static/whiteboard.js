import React, { useEffect, useMemo, useState } from "https://esm.sh/react@18.2.0";
import { createRoot } from "https://esm.sh/react-dom@18.2.0/client";
import { Excalidraw } from "https://esm.sh/@excalidraw/excalidraw@0.18.0?external=react,react-dom";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.4";

const config = window.WHITEBOARD_CONFIG;
const boardStrip = document.getElementById("wbBoardStrip");
const addBtn = document.getElementById("wbAddBoardBtn");
const saveStatusEl = document.getElementById("wbSaveStatus");
const presenceEl = document.getElementById("wbPresence");
const excalidrawRoot = document.getElementById("excalidrawRoot");

let boards = [];
let activeBoardId = null;
let excalidrawApi = null;
let sceneSet = null;
let debounceTimer = null;
let lastSavedSignature = "";
let applyingRemoteScene = false;
let realtimeChannel = null;
let presenceChannel = null;

const supabase = config.supabaseUrl && config.supabaseAnonKey
  ? createClient(config.supabaseUrl, config.supabaseAnonKey)
  : null;

if (supabase && config.supabaseAccessToken) {
  supabase.realtime.setAuth(config.supabaseAccessToken);
}

function setSaveStatus(text) {
  saveStatusEl.textContent = text;
}

function setPresenceText(text) {
  presenceEl.textContent = text;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || "Request failed");
  }
  return data;
}

function sceneSignature(scene) {
  return JSON.stringify(scene);
}

async function loadBoards() {
  const data = await api(`/api/whiteboard/${config.ownerUserId}/boards`);
  boards = data.boards || [];
  if (!boards.length) {
    setPresenceText("No boards available.");
    return;
  }
  if (!activeBoardId || !boards.find((b) => b.id === activeBoardId)) {
    activeBoardId = boards[0].id;
  }
  renderBoardStrip();
  await openBoard(activeBoardId);
}

function renderBoardStrip() {
  boardStrip.innerHTML = "";
  boards.forEach((board, idx) => {
    const item = document.createElement("div");
    item.className = `wb-board-item ${board.id === activeBoardId ? "active" : ""}`;

    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "wb-board-btn";
    openBtn.textContent = board.name;
    openBtn.onclick = () => openBoard(board.id);

    const renameBtn = document.createElement("button");
    renameBtn.type = "button";
    renameBtn.className = "wb-mini-btn";
    renameBtn.textContent = "✎";
    renameBtn.title = "Rename";
    renameBtn.onclick = async () => {
      const next = prompt("Rename board", board.name);
      if (next === null) return;
      await api(`/api/whiteboard/${config.ownerUserId}/boards/${board.id}`, {
        method: "PATCH",
        body: JSON.stringify({ name: next }),
      });
      await loadBoards();
    };

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "wb-mini-btn";
    delBtn.textContent = "✕";
    delBtn.title = "Delete";
    delBtn.onclick = async () => {
      if (!confirm("Delete this board?")) return;
      await api(`/api/whiteboard/${config.ownerUserId}/boards/${board.id}`, { method: "DELETE" });
      await loadBoards();
    };

    const leftBtn = document.createElement("button");
    leftBtn.type = "button";
    leftBtn.className = "wb-mini-btn";
    leftBtn.textContent = "←";
    leftBtn.disabled = idx === 0;
    leftBtn.onclick = async () => {
      const next = [...boards];
      [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
      await api(`/api/whiteboard/${config.ownerUserId}/boards/reorder`, {
        method: "PUT",
        body: JSON.stringify({ boardIds: next.map((b) => b.id) }),
      });
      await loadBoards();
    };

    const rightBtn = document.createElement("button");
    rightBtn.type = "button";
    rightBtn.className = "wb-mini-btn";
    rightBtn.textContent = "→";
    rightBtn.disabled = idx === boards.length - 1;
    rightBtn.onclick = async () => {
      const next = [...boards];
      [next[idx + 1], next[idx]] = [next[idx], next[idx + 1]];
      await api(`/api/whiteboard/${config.ownerUserId}/boards/reorder`, {
        method: "PUT",
        body: JSON.stringify({ boardIds: next.map((b) => b.id) }),
      });
      await loadBoards();
    };

    const actions = document.createElement("div");
    actions.className = "wb-board-item-actions";
    actions.append(leftBtn, rightBtn, renameBtn, delBtn);

    item.append(openBtn, actions);
    boardStrip.append(item);
  });
}

function mountScene(scene) {
  sceneSet = scene;
  if (!window.__wbRoot) {
    window.__wbRoot = createRoot(excalidrawRoot);
  }
  window.__wbRoot.render(
    React.createElement(WhiteboardCanvas, {
      boardId: activeBoardId,
      scene,
    })
  );
}

function normalizeScene(elements, appState, files) {
  return {
    elements,
    appState: {
      viewBackgroundColor: appState.viewBackgroundColor,
      zoom: appState.zoom,
      scrollX: appState.scrollX,
      scrollY: appState.scrollY,
      gridSize: appState.gridSize,
      theme: appState.theme,
    },
    files,
  };
}

async function saveScene(scene) {
  const signature = sceneSignature(scene);
  if (signature === lastSavedSignature) return;
  setSaveStatus("Saving...");
  await api(`/api/whiteboard/${config.ownerUserId}/boards/${activeBoardId}/state`, {
    method: "PUT",
    body: JSON.stringify({ scene }),
  });
  lastSavedSignature = signature;
  setSaveStatus("Saved");
}

function setupRealtime() {
  if (!supabase || !activeBoardId) return;

  if (realtimeChannel) supabase.removeChannel(realtimeChannel);
  if (presenceChannel) supabase.removeChannel(presenceChannel);

  realtimeChannel = supabase
    .channel(`board-updates-${activeBoardId}`)
    .on(
      "postgres_changes",
      {
        event: "UPDATE",
        schema: "public",
        table: "boards",
        filter: `id=eq.${activeBoardId}`,
      },
      (payload) => {
        const nextData = payload.new?.data_json;
        if (!nextData || applyingRemoteScene) return;
        let parsed;
        try {
          parsed = JSON.parse(nextData);
        } catch {
          return;
        }
        const nextSig = sceneSignature(parsed);
        if (nextSig === lastSavedSignature) return;
        applyingRemoteScene = true;
        lastSavedSignature = nextSig;
        mountScene(parsed);
        setSaveStatus("Synced");
        setTimeout(() => {
          applyingRemoteScene = false;
        }, 120);
      }
    )
    .subscribe();

  presenceChannel = supabase.channel(`board-presence-${activeBoardId}`, {
    config: { presence: { key: `${config.viewerName}-${Math.random().toString(36).slice(2, 7)}` } },
  });

  presenceChannel
    .on("presence", { event: "sync" }, () => {
      const state = presenceChannel.presenceState();
      const users = [];
      Object.keys(state).forEach((k) => {
        (state[k] || []).forEach((entry) => {
          if (entry?.name) users.push(entry.name);
        });
      });
      const unique = [...new Set(users)];
      setPresenceText(`Online: ${unique.length ? unique.join(", ") : "just you"}`);
    })
    .subscribe(async (status) => {
      if (status === "SUBSCRIBED") {
        await presenceChannel.track({ name: config.viewerName });
      }
      if (status === "CHANNEL_ERROR") {
        setPresenceText("Realtime unavailable");
      }
    });
}

async function openBoard(boardId) {
  activeBoardId = boardId;
  renderBoardStrip();
  const data = await api(`/api/whiteboard/${config.ownerUserId}/boards/${boardId}/state`);
  const scene = data.scene || { elements: [], appState: {}, files: {} };
  lastSavedSignature = sceneSignature(scene);
  mountScene(scene);
  setSaveStatus("Saved");
  setupRealtime();
}

function WhiteboardCanvas({ boardId, scene }) {
  const initialData = useMemo(() => scene, [boardId]);
  const [localScene, setLocalScene] = useState(initialData);

  useEffect(() => {
    setLocalScene(initialData);
  }, [initialData]);

  return React.createElement("div", { style: { width: "100%", height: "100%" } },
    React.createElement(Excalidraw, {
      key: boardId,
      initialData: localScene,
      onChange: (elements, appState, files) => {
        if (applyingRemoteScene) return;
        const scenePayload = normalizeScene(elements, appState, files);
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(async () => {
          try {
            await saveScene(scenePayload);
          } catch (err) {
            setSaveStatus(`Save failed: ${err.message}`);
          }
        }, 1200);
      },
      excalidrawAPI: (api) => {
        excalidrawApi = api;
      },
      viewModeEnabled: false,
      zenModeEnabled: false,
      gridModeEnabled: false,
      UIOptions: {
        canvasActions: { loadScene: false },
      },
    })
  );
}

addBtn.addEventListener("click", async () => {
  try {
    await api(`/api/whiteboard/${config.ownerUserId}/boards`, {
      method: "POST",
      body: JSON.stringify({ name: `Page ${boards.length + 1}` }),
    });
    await loadBoards();
    activeBoardId = boards[boards.length - 1]?.id || activeBoardId;
    renderBoardStrip();
    if (activeBoardId) await openBoard(activeBoardId);
  } catch (err) {
    setSaveStatus(`Create failed: ${err.message}`);
  }
});

(async function init() {
  try {
    setPresenceText("Connecting...");
    await loadBoards();
  } catch (err) {
    setPresenceText(`Failed to load boards: ${err.message}`);
    setSaveStatus("Unavailable");
  }
})();

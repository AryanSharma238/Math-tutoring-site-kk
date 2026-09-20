// Soft navigation: swaps page content in place so the embedded class call (#callWindow and its
// iframe) is never torn down by a full page load.
(function () {
  if (window.__softNavInit) return;
  window.__softNavInit = true;

  const scriptTextCache = {};

  function sameOrigin(url) { return url.origin === location.origin; }

  function loadExternal(src) {
    return new Promise((resolve) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = s.onerror = () => resolve();
      document.body.appendChild(s);
    });
  }

  async function runScripts(scripts) {
    for (const old of scripts) {
      const src = old.getAttribute("src");
      if (src) {
        if (src.includes("nav.js")) continue;
        if (src.includes("app.js")) {
          if (!scriptTextCache[src]) scriptTextCache[src] = await (await fetch(src)).text();
          runInline(scriptTextCache[src]);
        } else if (!document.querySelector(`script[src="${src}"]`)) {
          await loadExternal(src);
        }
      } else {
        runInline(old.textContent);
      }
    }
  }

  function runInline(text) {
    const s = document.createElement("script");
    s.textContent = `{\n${text}\n}`;
    document.body.appendChild(s);
    s.remove();
  }

  async function swapIn(html, url, { push, keepScroll }) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const keep = document.getElementById("callWindow");
    Array.from(document.body.children).forEach((el) => { if (el !== keep) el.remove(); });

    const scripts = [];
    Array.from(doc.body.children).forEach((el) => {
      if (el.id === "callWindow") return;
      if (el.tagName === "SCRIPT") { scripts.push(el); return; }
      const nested = el.querySelectorAll ? Array.from(el.querySelectorAll("script")) : [];
      nested.forEach((n) => { scripts.push(n); n.remove(); });
      document.body.appendChild(document.importNode(el, true));
    });
    // Scripts from the page's own script block sit after the content; keep source order.
    document.title = doc.title || document.title;
    if (push) history.pushState({}, "", url);
    if (!keepScroll) window.scrollTo(0, 0);

    await runScripts(scripts);

    document.querySelectorAll(".wb-embed[data-workspace-id]").forEach((el) => {
      if (el.offsetParent !== null && window.WB) WB.mount(el);
    });
  }

  async function softNavigate(url, opts = {}) {
    const target = new URL(url, location.href);
    try {
      const resp = await fetch(target.href, opts.fetchOptions || { credentials: "same-origin" });
      const type = resp.headers.get("content-type") || "";
      if (!type.includes("text/html") || resp.headers.get("content-disposition")) {
        location.href = target.href;
        return;
      }
      const finalUrl = resp.url || target.href;
      const html = await resp.text();
      await swapIn(html, finalUrl, {
        push: opts.push !== false && finalUrl !== location.href,
        keepScroll: !!opts.keepScroll,
      });
    } catch {
      location.href = target.href;
    }
  }

  window.softNavigate = softNavigate;
  window.softReload = () => softNavigate(location.href, { push: false, keepScroll: true });

  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest("a[href]");
    if (!a || (a.target && a.target !== "_self") || a.hasAttribute("download")) return;
    const url = new URL(a.href, location.href);
    if (!sameOrigin(url) || !/^https?:$/.test(url.protocol)) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    if (/^\/(logout|static)\b/.test(url.pathname)) return;
    e.preventDefault();
    softNavigate(url.href);
  });

  document.addEventListener("submit", async (e) => {
    if (e.defaultPrevented) return;
    const form = e.target;
    if (form.hasAttribute("data-native")) return;
    const action = new URL(form.getAttribute("action") || location.href, location.href);
    if (!sameOrigin(action) || /^\/logout\b/.test(action.pathname)) return;
    const method = (form.getAttribute("method") || "GET").toUpperCase();
    e.preventDefault();
    const fd = new FormData(form, e.submitter || undefined);
    if (method === "GET") {
      const qs = new URLSearchParams(fd).toString();
      action.search = qs;
      softNavigate(action.href);
    } else {
      softNavigate(action.href, { fetchOptions: { method, body: fd, credentials: "same-origin" } });
    }
  });

  window.addEventListener("popstate", () => softNavigate(location.href, { push: false }));
})();

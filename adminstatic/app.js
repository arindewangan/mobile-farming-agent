(function () {
  "use strict";

  const loginScreen = document.getElementById("login-screen");
  const mainScreen = document.getElementById("main-screen");
  let statusTimer = null;
  let lastStatus = null;

  function toast(msg, isErr) {
    const t = document.createElement("div");
    t.className = "toast" + (isErr ? " err" : "");
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }

  function fmtBytes(n) {
    if (!n || n <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  // -- sidebar navigation --------------------------------------------------
  document.querySelectorAll(".navitem").forEach((item) => {
    item.addEventListener("click", () => {
      const page = item.dataset.page;
      document.querySelectorAll(".navitem").forEach((n) => n.classList.toggle("active", n === item));
      document.querySelectorAll(".page").forEach((p) => p.classList.toggle("active", p.dataset.page === page));
    });
  });

  async function boot() {
    const s = await api("/api/session");
    if (!s.logged_in) {
      loginScreen.style.display = "block";
      mainScreen.style.display = "none";
      return;
    }
    loginScreen.style.display = "none";
    mainScreen.style.display = "flex";
    await Promise.all([refreshStatus(), loadConnections(), loadBans(), loadDevices(), loadLog(), loadUpdateInfo()]);
    if (statusTimer) clearInterval(statusTimer);
    statusTimer = setInterval(() => {
      refreshStatus();
      loadDevices();
      loadLog();
    }, 5000);
  }

  async function refreshStatus() {
    const s = await api("/api/status").catch(() => null);
    if (!s) return;
    lastStatus = s;

    const sidebar = document.getElementById("sidebar-status");
    sidebar.classList.toggle("live", !!s.connected);
    sidebar.innerHTML = `<span class="dot"></span>${s.connected ? `Connected — ${s.device_count} device(s)` : "Not connected"}`;

    document.getElementById("local-addr-box").textContent = s.local_addr || "— (couldn't detect a LAN IP)";
    document.getElementById("remote-addr-box").textContent = s.remote_addr || "— (Tailscale not detected)";

    const ts = s.tailscale || {};
    document.getElementById("ts-status").textContent = ts.installed
      ? (ts.online ? "up" : "installed, not connected")
      : "not installed / not detected";
    document.getElementById("ts-hostname").textContent = ts.hostname || "—";
    const peers = ts.peers || [];
    document.getElementById("ts-peers").textContent = ts.installed
      ? `${peers.filter((p) => p.online).length} / ${peers.length}`
      : "—";

    const toggle = document.getElementById("allow-remote-toggle");
    if (document.activeElement !== toggle) toggle.checked = !!s.allow_remote;

    const peerWrap = document.getElementById("peer-wrap");
    if (s.current_peer) {
      const p = s.current_peer;
      const since = p.connected_since ? new Date(p.connected_since * 1000).toLocaleTimeString() : "?";
      peerWrap.innerHTML = `<div class="peer-card">
        <div class="row between">
          <div>
            Connected from <span class="mono">${escapeHtml(p.ip || "?")}</span>
            <span class="badge badge-peer-${p.scope}">${p.scope}</span>
            via <b>${escapeHtml(p.connection_name || "?")}</b>
            <span class="subtle">since ${since}</span>
          </div>
          <div class="row" style="gap:8px">
            <button class="secondary sm" id="disconnect-btn">Disconnect</button>
            <button class="danger sm" id="ban-current-btn">Ban &amp; disconnect</button>
          </div>
        </div>
      </div>`;
      document.getElementById("disconnect-btn").addEventListener("click", async () => {
        await api("/api/kick", { method: "POST" }).catch((e) => toast(e.message, true));
        refreshStatus();
      });
      document.getElementById("ban-current-btn").addEventListener("click", async () => {
        if (!confirm(`Ban ${p.ip}? It won't be able to connect again until unbanned.`)) return;
        await api("/api/bans/current", { method: "POST" }).catch((e) => toast(e.message, true));
        refreshStatus();
        loadBans();
      });
    } else {
      peerWrap.innerHTML = "";
    }

    const observersWrap = document.getElementById("observers-wrap");
    const observers = s.observers || [];
    if (observers.length) {
      observersWrap.innerHTML = `<div class="section-title">Additional (read-only) connections</div>` + observers.map((o) => {
        const since = o.connected_since ? new Date(o.connected_since * 1000).toLocaleTimeString() : "?";
        return `<div class="peer-card">
          <div class="row between">
            <div>
              Connected from <span class="mono">${escapeHtml(o.ip || "?")}</span>
              <span class="badge badge-peer-${o.scope}">${o.scope}</span>
              via <b>${escapeHtml(o.connection_name || "?")}</b>
              <span class="subtle">since ${since} · read-only, can't send commands</span>
            </div>
            <button class="secondary sm observer-disconnect-btn" data-cid="${o.connection_id}">Disconnect</button>
          </div>
        </div>`;
      }).join("");
      observersWrap.querySelectorAll(".observer-disconnect-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          await api(`/api/observers/${encodeURIComponent(btn.dataset.cid)}/kick`, { method: "POST" }).catch((e) => toast(e.message, true));
          refreshStatus();
        });
      });
    } else {
      observersWrap.innerHTML = "";
    }

    const traffic = s.traffic || {};
    const wrap = document.getElementById("traffic-wrap");
    const ifaces = traffic.interfaces || [];
    wrap.innerHTML = `
      <div class="kv"><span class="k">This session — sent</span><span class="v">${fmtBytes(traffic.session_bytes_sent)}</span></div>
      <div class="kv"><span class="k">This session — received</span><span class="v">${fmtBytes(traffic.session_bytes_received)}</span></div>
      ${ifaces.length ? `<div class="section-title">Interfaces (lifetime totals)</div>
      <div class="tablewrap"><table><thead><tr><th>Interface</th><th>Received</th><th>Sent</th></tr></thead>
      <tbody>${ifaces.map((i) => `<tr><td class="mono">${escapeHtml(i.iface)}</td><td>${fmtBytes(i.rx_bytes)}</td><td>${fmtBytes(i.tx_bytes)}</td></tr>`).join("")}</tbody></table></div>` : ""}
    `;

    updateConnectedBadge();
  }

  // Keeps the "connected now" badge in sync on every poll WITHOUT rebuilding
  // the connections table — a full rebuild would wipe any token a user just
  // revealed (or the show/hide toggle state) out from under them mid-copy.
  function updateConnectedBadge() {
    const wrap = document.getElementById("connections-wrap");
    const activeId = lastStatus?.current_peer?.connection_id;
    const observerIds = new Set((lastStatus?.observers || []).map((o) => o.connection_id));
    wrap.querySelectorAll("tr[data-id]").forEach((tr) => {
      const id = Number(tr.dataset.id);
      const nameCell = tr.querySelector("td:first-child");
      if (!nameCell) return;
      const existingBadge = nameCell.querySelector(".badge-live");
      const existingObsBadge = nameCell.querySelector(".badge-observing");
      if (id === activeId && !existingBadge) {
        nameCell.insertAdjacentHTML("beforeend", ' <span class="badge badge-live">connected now</span>');
      } else if (id !== activeId && existingBadge) {
        existingBadge.remove();
      }
      if (id !== activeId && observerIds.has(id) && !existingObsBadge) {
        nameCell.insertAdjacentHTML("beforeend", ' <span class="badge badge-observing">observing now</span>');
      } else if (!observerIds.has(id) && existingObsBadge) {
        existingObsBadge.remove();
      }
    });
  }

  // -- software update (checks the agent's GitHub release directly) -------
  async function loadUpdateInfo() {
    const wrap = document.getElementById("update-wrap");
    wrap.innerHTML = `<div class="subtle">Checking…</div>`;
    let info;
    try {
      info = await api("/api/update/check");
    } catch (e) {
      wrap.innerHTML = `<div class="subtle">Couldn't check for updates: ${escapeHtml(e.message)}</div>`;
      return;
    }
    document.getElementById("update-current").textContent = info.current || "—";
    if (!info.ok) {
      wrap.innerHTML = `<div class="subtle">Couldn't check for updates: ${escapeHtml(info.error || "unknown error")}</div>`;
      return;
    }
    if (!info.update_available) {
      wrap.innerHTML = `<div class="subtle">Up to date.</div>`;
      return;
    }
    wrap.innerHTML = `<div class="peer-card">
      <div class="row between">
        <div>
          <b>Update available: v${escapeHtml(info.latest)}</b>
          ${info.url ? ` <a href="${escapeHtml(info.url)}" target="_blank" rel="noopener">release notes</a>` : ""}
        </div>
        <button id="update-apply-btn">Update now</button>
      </div>
    </div>`;
    document.getElementById("update-apply-btn").addEventListener("click", async () => {
      if (!confirm(`Download and apply v${info.latest}? The agent restarts when done; the previous version is kept as a backup you can roll back to.`)) return;
      const btn = document.getElementById("update-apply-btn");
      btn.disabled = true;
      btn.textContent = "Updating…";
      try {
        await api("/api/update/apply", { method: "POST" });
        toast("Update applied — agent is restarting");
        document.getElementById("update-wrap").innerHTML = `<div class="subtle">Restarting with v${escapeHtml(info.latest)}…</div>`;
      } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
        btn.textContent = "Update now";
      }
    });
  }
  document.getElementById("update-check-btn").addEventListener("click", loadUpdateInfo);

  document.getElementById("allow-remote-toggle").addEventListener("change", async (e) => {
    const checked = e.target.checked;
    e.target.disabled = true;
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify({ allow_remote: checked }) });
      toast(checked ? "Remote connections allowed" : "Remote connections blocked");
    } catch (err) {
      e.target.checked = !checked;
      toast(err.message, true);
    } finally {
      e.target.disabled = false;
    }
  });

  document.getElementById("login-btn").addEventListener("click", async () => {
    const password = document.getElementById("login-password").value.trim();
    if (!password) return;
    try {
      await api("/api/login", { method: "POST", body: JSON.stringify({ password }) });
      await boot();
    } catch (e) {
      toast("Invalid password", true);
    }
  });
  document.getElementById("login-password").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("login-btn").click();
  });

  document.getElementById("cp-btn").addEventListener("click", async () => {
    const oldInput = document.getElementById("cp-old");
    const newInput = document.getElementById("cp-new");
    const confirmInput = document.getElementById("cp-confirm");
    const old_password = oldInput.value;
    const new_password = newInput.value;
    if (!old_password || !new_password) return;
    if (new_password.length < 8) { toast("New password must be at least 8 characters", true); return; }
    if (new_password !== confirmInput.value) { toast("New passwords don't match", true); return; }
    try {
      await api("/api/change-password", { method: "POST", body: JSON.stringify({ old_password, new_password }) });
      oldInput.value = "";
      newInput.value = "";
      confirmInput.value = "";
      toast("Password changed");
    } catch (e) {
      toast(e.message, true);
    }
  });

  document.getElementById("logout-btn").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" });
    if (statusTimer) clearInterval(statusTimer);
    await boot();
  });

  function copyText(text) {
    navigator.clipboard.writeText(text).then(() => toast("Copied")).catch(() => toast("Copy failed", true));
  }

  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", () => copyText(document.getElementById(btn.dataset.copy).textContent));
  });

  document.getElementById("create-btn").addEventListener("click", async () => {
    const nameInput = document.getElementById("new-name");
    const scope = document.getElementById("new-scope").value;
    const name = nameInput.value.trim();
    if (!name) return;
    try {
      const row = await api("/api/connections", { method: "POST", body: JSON.stringify({ name, scope }) });
      nameInput.value = "";
      const banner = document.getElementById("new-token-banner");
      banner.style.display = "block";
      banner.innerHTML = `<b>${escapeHtml(row.name)}</b> created — copy its token now, it won't be shown again in full:<br/>
        <div class="tokenbox" style="margin-top:8px"><span class="mono" id="fresh-token">${escapeHtml(row.token)}</span>
        <button class="icbtn" data-copy="fresh-token">copy</button></div>`;
      banner.querySelector("[data-copy]").addEventListener("click", () => copyText(row.token));
      await loadConnections();
    } catch (e) {
      toast(e.message, true);
    }
  });

  async function loadConnections() {
    const r = await api("/api/connections").catch(() => null);
    const wrap = document.getElementById("connections-wrap");
    const rows = r?.connections ?? [];
    if (!rows.length) {
      wrap.innerHTML = `<div class="empty">No connections yet — create one above.</div>`;
      return;
    }
    const activeId = lastStatus?.current_peer?.connection_id;
    wrap.innerHTML = `<table>
      <thead><tr><th>Name</th><th>Token</th><th>Scope</th><th>Status</th><th>Last seen</th><th>Last from</th><th></th></tr></thead>
      <tbody>${rows.map((c) => `
        <tr data-id="${c.id}">
          <td>${escapeHtml(c.name)}${c.id === activeId ? ' <span class="badge badge-live">connected now</span>' : ""}</td>
          <td>
            <div class="tokenbox" style="padding:4px 6px">
              <span class="mono token-cell" data-id="${c.id}" data-revealed="false">${escapeHtml(c.token_preview)}</span>
              <button class="icbtn show-token-btn" data-id="${c.id}">show</button>
              <button class="icbtn copy-token-btn" data-id="${c.id}">copy</button>
            </div>
          </td>
          <td>
            <select class="scope-select" data-id="${c.id}">
              <option value="both" ${c.scope === "both" ? "selected" : ""}>local + remote</option>
              <option value="local" ${c.scope === "local" ? "selected" : ""}>local only</option>
              <option value="remote" ${c.scope === "remote" ? "selected" : ""}>remote only</option>
            </select>
          </td>
          <td><span class="badge ${c.enabled ? "badge-online" : "badge-offline"}">${c.enabled ? "enabled" : "disabled"}</span></td>
          <td class="subtle">${c.last_seen ? new Date(c.last_seen * 1000).toLocaleString() : "never"}</td>
          <td class="subtle mono">${c.last_ip ? `${escapeHtml(c.last_ip)} (${escapeHtml(c.last_scope || "?")})` : "—"}</td>
          <td class="row">
            <button class="secondary sm toggle-btn">${c.enabled ? "Disable" : "Enable"}</button>
            <button class="danger sm delete-btn">Delete</button>
          </td>
        </tr>`).join("")}</tbody>
    </table>`;

    wrap.querySelectorAll(".show-token-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        const cell = wrap.querySelector(`.token-cell[data-id="${id}"]`);
        if (cell.dataset.revealed === "true") {
          const row = rows.find((r) => String(r.id) === id);
          cell.textContent = row.token_preview;
          cell.dataset.revealed = "false";
          btn.textContent = "show";
          return;
        }
        try {
          const { token } = await api(`/api/connections/${id}/token`);
          cell.textContent = token;
          cell.dataset.revealed = "true";
          btn.textContent = "hide";
        } catch (e) {
          toast(e.message, true);
        }
      });
    });
    wrap.querySelectorAll(".copy-token-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        try {
          const { token } = await api(`/api/connections/${id}/token`);
          copyText(token);
        } catch (e) {
          toast(e.message, true);
        }
      });
    });
    wrap.querySelectorAll(".toggle-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.closest("tr").dataset.id;
        const row = rows.find((r) => String(r.id) === id);
        await api(`/api/connections/${id}/toggle`, { method: "POST", body: JSON.stringify({ enabled: !row.enabled }) }).catch((e) => toast(e.message, true));
        loadConnections();
      });
    });
    wrap.querySelectorAll(".delete-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Delete this connection? The dashboard using it will stop working.")) return;
        const id = btn.closest("tr").dataset.id;
        await api(`/api/connections/${id}`, { method: "DELETE" }).catch((e) => toast(e.message, true));
        loadConnections();
      });
    });
    wrap.querySelectorAll(".scope-select").forEach((sel) => {
      sel.addEventListener("change", async () => {
        const id = sel.dataset.id;
        await api(`/api/connections/${id}/scope`, { method: "POST", body: JSON.stringify({ scope: sel.value }) })
          .then(() => toast("Scope updated"))
          .catch((e) => { toast(e.message, true); loadConnections(); });
      });
    });
  }

  document.getElementById("ban-btn").addEventListener("click", async () => {
    const ipInput = document.getElementById("ban-ip");
    const reasonInput = document.getElementById("ban-reason");
    const ip = ipInput.value.trim();
    if (!ip) return;
    try {
      await api("/api/bans", { method: "POST", body: JSON.stringify({ ip, reason: reasonInput.value.trim() }) });
      ipInput.value = "";
      reasonInput.value = "";
      await loadBans();
      toast("Banned");
    } catch (e) {
      toast(e.message, true);
    }
  });

  async function loadBans() {
    const r = await api("/api/bans").catch(() => null);
    const wrap = document.getElementById("bans-wrap");
    const rows = r?.bans ?? [];
    if (!rows.length) {
      wrap.innerHTML = `<div class="empty">No banned addresses.</div>`;
      return;
    }
    wrap.innerHTML = `<table>
      <thead><tr><th>IP</th><th>Reason</th><th>Banned at</th><th></th></tr></thead>
      <tbody>${rows.map((b) => `
        <tr data-ip="${escapeHtml(b.ip)}">
          <td class="mono">${escapeHtml(b.ip)}</td>
          <td class="subtle">${escapeHtml(b.reason || "—")}</td>
          <td class="subtle">${new Date(b.banned_at * 1000).toLocaleString()}</td>
          <td><button class="secondary sm unban-btn">Unban</button></td>
        </tr>`).join("")}</tbody>
    </table>`;
    wrap.querySelectorAll(".unban-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const ip = btn.closest("tr").dataset.ip;
        await api(`/api/bans/${encodeURIComponent(ip)}`, { method: "DELETE" }).catch((e) => toast(e.message, true));
        loadBans();
      });
    });
  }

  async function loadLog() {
    const r = await api("/api/connection-log").catch(() => null);
    const wrap = document.getElementById("log-wrap");
    const rows = r?.log ?? [];
    if (!rows.length) {
      wrap.innerHTML = `<div class="empty">No connection attempts recorded yet.</div>`;
      return;
    }
    wrap.innerHTML = `<table>
      <thead><tr><th>When</th><th>IP</th><th>Scope</th><th>Via</th><th>Result</th></tr></thead>
      <tbody>${rows.map((l) => `
        <tr>
          <td class="subtle">${new Date(l.at * 1000).toLocaleString()}</td>
          <td class="mono">${escapeHtml(l.ip || "?")}</td>
          <td>${l.scope && l.scope !== "?" ? `<span class="badge badge-peer-${l.scope}">${l.scope}</span>` : "—"}</td>
          <td class="subtle">${escapeHtml(l.connection_name || "—")}</td>
          <td><span class="badge badge-${l.outcome}">${escapeHtml(l.outcome)}</span>${l.reason ? ` <span class="subtle">${escapeHtml(l.reason)}</span>` : ""}</td>
        </tr>`).join("")}</tbody>
    </table>`;
  }

  async function loadDevices() {
    const r = await api("/api/adb-devices").catch(() => null);
    const wrap = document.getElementById("devices-wrap");
    const rows = r?.devices ?? [];
    if (!rows.length) {
      wrap.innerHTML = `<div class="empty">No ADB devices detected.</div>`;
      return;
    }
    wrap.innerHTML = `<table>
      <thead><tr><th>Serial</th><th>Model</th><th>Android</th><th>SDK</th><th>State</th><th>Visibility</th><th></th></tr></thead>
      <tbody>${rows.map((d) => `
        <tr data-serial="${escapeHtml(d.serial)}">
          <td class="mono">${escapeHtml(d.serial)}</td>
          <td>${escapeHtml(d.model || "unknown")}</td>
          <td>${escapeHtml(d.android || "?")}</td>
          <td>${escapeHtml(d.sdk || "?")}</td>
          <td><span class="badge ${d.state === "device" ? "badge-online" : "badge-offline"}">${escapeHtml(d.state || "?")}</span></td>
          <td><span class="badge ${d.hidden ? "badge-hidden" : "badge-online"}">${d.hidden ? "hidden" : "visible"}</span></td>
          <td><button class="secondary sm visibility-btn">${d.hidden ? "Show on dashboard" : "Hide from dashboard"}</button></td>
        </tr>`).join("")}</tbody>
    </table>`;
    wrap.querySelectorAll(".visibility-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const tr = btn.closest("tr");
        const serial = tr.dataset.serial;
        const row = rows.find((d) => d.serial === serial);
        await api(`/api/adb-devices/${serial}/visibility`, { method: "POST", body: JSON.stringify({ hidden: !row.hidden }) })
          .then(() => loadDevices())
          .catch((e) => toast(e.message, true));
      });
    });
  }

  boot();
})();

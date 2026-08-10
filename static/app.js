const state = {
  token: localStorage.getItem("whisper_token"),
  me: null,
  contacts: [],
  blocked: new Set(),
  rooms: [],
  active: null,       // active 1:1 conversation
  activeRoom: null,   // active room
  unread: {},
  socket: null,
};

const $ = (selector) => document.querySelector(selector);
const loginView = $("#login-view");
const chatView = $("#chat-view");
let installPrompt = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
      ...options.headers,
    },
  });
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    const error = new Error(data.detail || "Something went wrong");
    error.status = response.status;
    throw error;
  }
  return data;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("visible"), 3200);
}

function initials(username) {
  return username.slice(0, 2).toUpperCase();
}

function setAuthenticated(authenticated) {
  loginView.classList.toggle("hidden", authenticated);
  chatView.classList.toggle("hidden", !authenticated);
}

async function login(username, password, shouldReserve) {
  const session = await api("/api/session", {
    method: "POST",
    body: JSON.stringify({ username, ...(password ? { password } : {}) }),
  });
  state.token = session.token;
  state.me = session;
  localStorage.setItem("whisper_token", state.token);
  if (shouldReserve && !session.reserved) {
    if (!password) throw new Error("Enter a password with at least 8 characters");
    await api("/api/session/reserve", {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    state.me.reserved = true;
  }
  enterApp();
}

async function restoreSession() {
  if (!state.token) return setAuthenticated(false);
  try {
    state.me = await api("/api/session");
    enterApp();
  } catch (error) {
    if (error.status === 401) {
      localStorage.removeItem("whisper_token");
      state.token = null;
    }
    setAuthenticated(false);
  }
}

async function enterApp() {
  setAuthenticated(true);
  $("#current-username").textContent = `@${state.me.username}`;
  updateReservationUI();
  await Promise.all([loadContacts(), loadBlocked(), loadRooms()]);
  connectSocket();
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
  }
}

function updateReservationUI() {
  $("#reserve-form").classList.toggle("hidden", Boolean(state.me.reserved));
  $("#reserve-status").textContent = state.me.reserved ? "Reserved" : "";
}

async function loadContacts() {
  state.contacts = await api("/api/contacts");
  renderContacts();
}

async function loadBlocked() {
  const list = await api("/api/blocked");
  state.blocked = new Set(list.map((u) => u.username.toLowerCase()));
  updateBlockButton();
}

async function loadRooms() {
  state.rooms = await api("/api/rooms");
  renderRooms();
}

function renderRooms() {
  const container = $("#rooms");
  container.replaceChildren();
  for (const room of state.rooms) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `contact room-item ${state.activeRoom?.room_code === room.room_code ? "active" : ""}`;
    button.dataset.roomCode = room.room_code;
    button.innerHTML = `<span class="contact-avatar room-avatar">#</span><div class="room-meta"><span>${room.display_name}</span><span class="room-alias">as ${room.alias}</span></div>`;
    button.addEventListener("click", () => openRoom(room.room_code, room.display_name, room.alias));
    container.append(button);
  }
  $("#empty-rooms").classList.toggle("hidden", state.rooms.length > 0);
}

function renderContacts() {
  const container = $("#contacts");
  container.replaceChildren();
  for (const contact of state.contacts) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `contact ${state.active?.with_user.username === contact.username ? "active" : ""}`;
    const unread = state.unread[contact.username] || 0;
    button.innerHTML = `<span class="contact-avatar">${initials(contact.username)}</span><span>${contact.username}</span>${unread ? `<span class="unread">${unread}</span>` : ""}`;
    button.addEventListener("click", () => openConversation(contact.username));
    container.append(button);
  }
  $("#contact-count").textContent = state.contacts.length;
  $("#empty-contacts").classList.toggle("hidden", state.contacts.length > 0);
}

async function openConversation(username) {
  try {
    state.active = await api("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ username }),
    });
    state.activeRoom = null;
    state.unread[username] = 0;
    $("#find-error").textContent = "";
    $("#find-username").value = "";
    $("#chat-username").textContent = username;
    $("#chat-avatar").textContent = initials(username);
    $("#expiry-select").value = String(state.active.expiry_hours);
    $("#conversation").classList.remove("hidden");
    $("#expiry-select").parentElement.classList.remove("hidden");
    $("#save-button").classList.remove("hidden");
    $("#block-button").classList.remove("hidden");
    $("#report-button").classList.remove("hidden");
    $("#empty-chat").classList.add("hidden");
    chatView.classList.add("chat-open");
    updateSaveButton();
    updateBlockButton();
    renderContacts();
    await loadMessages();
    $("#message-input").focus();
  } catch (error) {
    $("#find-error").textContent = error.message;
  }
}

async function openRoom(roomCode, displayName, alias) {
  try {
    state.activeRoom = { room_code: roomCode, display_name: displayName, alias };
    state.active = null;
    $("#find-error").textContent = "";
    $("#chat-username").textContent = displayName;
    $("#chat-avatar").textContent = "#";
    $("#conversation").classList.remove("hidden");
    $("#expiry-select").parentElement.classList.add("hidden");
    $("#save-button").classList.add("hidden");
    $("#block-button").classList.add("hidden");
    $("#report-button").classList.add("hidden");
    $("#blocked-notice").classList.add("hidden");
    $("#empty-chat").classList.add("hidden");
    chatView.classList.add("chat-open");
    renderRooms();
    await loadRoomMessages();
    $("#message-input").focus();
  } catch (error) {
    showToast(error.message);
  }
}

async function loadMessages() {
  const messages = await api(`/api/conversations/${state.active.id}/messages`);
  const container = $("#messages");
  container.replaceChildren();
  messages.forEach(appendMessage);
  container.scrollTop = container.scrollHeight;
  updateBlockedNotice();
}

async function loadRoomMessages() {
  const messages = await api(`/api/rooms/${state.activeRoom.room_code}/messages`);
  const container = $("#messages");
  container.replaceChildren();
  messages.forEach(appendRoomMessage);
  container.scrollTop = container.scrollHeight;
}

function appendMessage(message) {
  if (document.querySelector(`[data-message-id="${message.id}"]`)) return;
  const mine = message.sender.toLowerCase() === state.me.username.toLowerCase();
  const element = document.createElement("article");
  element.className = `message ${mine ? "mine" : ""}`;
  element.dataset.messageId = message.id;
  const body = document.createElement("div");
  body.className = "bubble";
  body.textContent = message.body;
  const meta = document.createElement("div");
  meta.className = "message-meta";
  const sent = new Date(message.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const expires = new Date(message.expires_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  meta.innerHTML = `<span>${mine ? "you" : message.sender} · ${sent}</span><span>erases ${expires}</span>`;
  element.append(body, meta);
  $("#messages").append(element);
  $("#messages").scrollTop = $("#messages").scrollHeight;
}

function appendRoomMessage(message) {
  if (document.querySelector(`[data-room-message-id="${message.id}"]`)) return;
  const element = document.createElement("article");
  element.className = `message ${message.is_mine ? "mine" : ""}`;
  element.dataset.roomMessageId = message.id;
  const body = document.createElement("div");
  body.className = "bubble";
  body.textContent = message.body;
  const meta = document.createElement("div");
  meta.className = "message-meta";
  const sent = new Date(message.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const label = message.is_mine ? `You (${message.sender_alias})` : message.sender_alias;
  meta.innerHTML = `<span>${label} · ${sent}</span>`;
  element.append(body, meta);
  $("#messages").append(element);
  $("#messages").scrollTop = $("#messages").scrollHeight;
}

function updateBlockButton() {
  if (!state.active) return;
  const username = state.active.with_user?.username;
  if (!username) return;
  const isBlocked = state.blocked.has(username.toLowerCase());
  const btn = $("#block-button");
  btn.querySelector("span").textContent = isBlocked ? "Unblock" : "Block";
  btn.title = isBlocked ? "Unblock this user" : "Block this user";
  btn.classList.toggle("blocked", isBlocked);
  updateBlockedNotice();
}

function updateBlockedNotice() {
  if (!state.active) return;
  const username = state.active.with_user?.username;
  const isBlocked = username && state.blocked.has(username.toLowerCase());
  $("#blocked-notice").classList.toggle("hidden", !isBlocked);
}

function updateSaveButton() {
  const saved = state.contacts.some((contact) => contact.username.toLowerCase() === state.active.with_user.username.toLowerCase());
  $("#save-button").classList.toggle("saved", saved);
  $("#save-button span").textContent = saved ? "Saved" : "Save";
  $("#save-button").disabled = saved;
}

function connectSocket() {
  if (state.socket) state.socket.close();
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  state.socket = new WebSocket(`${protocol}://${location.host}/ws?token=${encodeURIComponent(state.token)}`);
  state.socket.onmessage = ({ data }) => {
    const event = JSON.parse(data);
    if (event.type === "room_message") {
      const msg = event.message;
      if (state.activeRoom?.room_code === msg.room_code) {
        appendRoomMessage(msg);
      }
      showToast(`New message in room ${msg.room_code}`);
      return;
    }
    if (event.type !== "message") return;
    const message = event.message;
    if (state.active?.id === message.conversation_id) {
      appendMessage(message);
    } else {
      state.unread[message.sender] = (state.unread[message.sender] || 0) + 1;
      renderContacts();
    }
    showToast(`New message from ${message.sender}`);
    if (document.hidden && "Notification" in window && Notification.permission === "granted") {
      new Notification(`Message from ${message.sender}`, { body: message.body, tag: `chat-${message.conversation_id}` });
    }
  };
  state.socket.onclose = () => {
    if (state.token) setTimeout(connectSocket, 2000);
  };
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  try {
    const form = new FormData(event.currentTarget);
    await login(form.get("username"), form.get("password"), form.get("reserve") === "on");
  } catch (error) {
    $("#login-error").textContent = error.message;
  }
});

$("#reserve-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = new FormData(event.currentTarget).get("password");
  try {
    await api("/api/session/reserve", {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    state.me.reserved = true;
    event.currentTarget.reset();
    updateReservationUI();
    showToast("Username reserved");
  } catch (error) {
    $("#reserve-status").textContent = error.message;
  }
});

$("#find-form").addEventListener("submit", (event) => {
  event.preventDefault();
  openConversation(new FormData(event.currentTarget).get("username"));
});

$("#message-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#message-input");
  if (!input.value.trim()) return;
  try {
    if (state.activeRoom) {
      const message = await api(`/api/rooms/${state.activeRoom.room_code}/messages`, {
        method: "POST",
        body: JSON.stringify({ body: input.value }),
      });
      appendRoomMessage(message);
    } else {
      const message = await api(`/api/conversations/${state.active.id}/messages`, {
        method: "POST",
        body: JSON.stringify({ body: input.value }),
      });
      appendMessage(message);
    }
    input.value = "";
    input.style.height = "auto";
  } catch (error) {
    showToast(error.message);
  }
});

$("#message-input").addEventListener("input", (event) => {
  event.target.style.height = "auto";
  event.target.style.height = `${Math.min(event.target.scrollHeight, 130)}px`;
});

$("#message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#message-form").requestSubmit();
  }
});

$("#save-button").addEventListener("click", async () => {
  await api("/api/contacts", {
    method: "POST",
    body: JSON.stringify({ username: state.active.with_user.username }),
  });
  await loadContacts();
  updateSaveButton();
  showToast("Username saved");
});

$("#block-button").addEventListener("click", async () => {
  if (!state.active) return;
  const username = state.active.with_user.username;
  const isBlocked = state.blocked.has(username.toLowerCase());
  try {
    if (isBlocked) {
      await api("/api/unblock", { method: "POST", body: JSON.stringify({ username }) });
      state.blocked.delete(username.toLowerCase());
      showToast(`Unblocked ${username}`);
    } else {
      await api("/api/block", { method: "POST", body: JSON.stringify({ username }) });
      state.blocked.add(username.toLowerCase());
      showToast(`Blocked ${username}`);
    }
    updateBlockButton();
  } catch (error) {
    showToast(error.message);
  }
});

$("#report-button").addEventListener("click", () => {
  if (!state.active) return;
  $("#report-dialog").showModal();
});

$("#report-cancel").addEventListener("click", () => $("#report-dialog").close());

$("#report-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.submitter?.value !== "submit") return;
  const username = state.active.with_user.username;
  const form = new FormData(event.currentTarget);
  try {
    await api("/api/report", {
      method: "POST",
      body: JSON.stringify({
        username,
        reason: form.get("reason"),
        details: form.get("details") || undefined,
      }),
    });
    event.currentTarget.reset();
    $("#report-dialog").close();
    showToast("Report submitted. Thank you.");
  } catch (error) {
    showToast(error.message);
  }
});

$("#unblock-inline").addEventListener("click", async () => {
  if (!state.active) return;
  const username = state.active.with_user.username;
  try {
    await api("/api/unblock", { method: "POST", body: JSON.stringify({ username }) });
    state.blocked.delete(username.toLowerCase());
    updateBlockButton();
    showToast(`Unblocked ${username}`);
  } catch (error) {
    showToast(error.message);
  }
});

$("#expiry-select").addEventListener("change", async (event) => {
  const updated = await api(`/api/conversations/${state.active.id}`, {
    method: "PATCH",
    body: JSON.stringify({ expiry_hours: Number(event.target.value) }),
  });
  state.active.expiry_hours = updated.expiry_hours;
  showToast(`New messages erase after ${event.target.selectedOptions[0].text.toLowerCase()}`);
});

$("#back-button").addEventListener("click", () => chatView.classList.remove("chat-open"));
$("#install-button").addEventListener("click", async () => {
  if (!installPrompt) return;
  installPrompt.prompt();
  const choice = await installPrompt.userChoice;
  installPrompt = null;
  $("#install-button").classList.add("hidden");
  if (choice.outcome === "accepted") showToast("Whisper installed");
});
$("#logout-button").addEventListener("click", () => {
  state.token = null;
  state.socket?.close();
  localStorage.removeItem("whisper_token");
  location.reload();
});

$("#create-room-button").addEventListener("click", () => {
  $("#create-room-dialog").showModal();
});
$("#create-room-cancel").addEventListener("click", () => $("#create-room-dialog").close());

$("#create-room-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.submitter?.value !== "submit") return;
  const form = new FormData(event.currentTarget);
  const body = { display_name: form.get("display_name"), default_message_lifetime_hours: Number(form.get("default_message_lifetime_hours")) };
  const expiresIn = form.get("expires_in_hours");
  if (expiresIn) body.expires_in_hours = Number(expiresIn);
  try {
    const room = await api("/api/rooms", { method: "POST", body: JSON.stringify(body) });
    state.rooms.unshift({ room_code: room.room_code, display_name: room.display_name, alias: room.your_alias });
    renderRooms();
    event.currentTarget.reset();
    $("#create-room-dialog").close();
    showToast(`Room created: ${room.room_code}`);
    openRoom(room.room_code, room.display_name, room.your_alias);
  } catch (error) {
    showToast(error.message);
  }
});

$("#join-room-button").addEventListener("click", () => {
  $("#join-room-error").textContent = "";
  $("#join-room-dialog").showModal();
});
$("#join-room-cancel").addEventListener("click", () => $("#join-room-dialog").close());

$("#join-room-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.submitter?.value !== "submit") return;
  const form = new FormData(event.currentTarget);
  try {
    const room = await api("/api/rooms/join", {
      method: "POST",
      body: JSON.stringify({ room_code: form.get("room_code") }),
    });
    if (!state.rooms.find((r) => r.room_code === room.room_code)) {
      state.rooms.unshift({ room_code: room.room_code, display_name: room.display_name, alias: room.your_alias });
      renderRooms();
    }
    event.currentTarget.reset();
    $("#join-room-dialog").close();
    showToast(`Joined ${room.display_name} as ${room.your_alias}`);
    openRoom(room.room_code, room.display_name, room.your_alias);
  } catch (error) {
    $("#join-room-error").textContent = error.message;
  }
});

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  $("#install-button").classList.remove("hidden");
});

window.addEventListener("appinstalled", () => {
  installPrompt = null;
  $("#install-button").classList.add("hidden");
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/service-worker.js"));
}

restoreSession();


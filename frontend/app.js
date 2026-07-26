const API = "/api";

let activeSubjectId = null;
let pollTimer = null;

const subjectListEl = document.getElementById("subject-list");
const emptyStateEl = document.getElementById("empty-state");
const subjectViewEl = document.getElementById("subject-view");
const subjectTitleEl = document.getElementById("subject-title");
const materialsBodyEl = document.getElementById("materials-body");
const chatMessagesEl = document.getElementById("chat-messages");

// Chat history lives client-side only, per subject, for this session -
// not persisted server-side yet.
const chatHistoryBySubject = {};

async function api(path, options) {
  const res = await fetch(API + path, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed: ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

// fetch() can't report upload progress, so file uploads use XHR instead.
function uploadFileWithProgress(path, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", API + path);
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    });
    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText));
        return;
      }
      let message = `Request failed: ${xhr.status}`;
      try {
        message = JSON.parse(xhr.responseText).detail || message;
      } catch (_) {
        // ignore non-JSON error bodies
      }
      reject(new Error(message));
    });
    xhr.addEventListener("error", () => reject(new Error("Network error during upload")));
    const formData = new FormData();
    formData.append("file", file);
    xhr.send(formData);
  });
}

async function loadSubjects() {
  const subjects = await api("/subjects");
  subjectListEl.innerHTML = "";
  for (const subject of subjects) {
    const li = document.createElement("li");
    li.className = subject.id === activeSubjectId ? "active" : "";
    li.innerHTML = `<span>${escapeHtml(subject.name)}</span><span class="count">${subject.material_count}</span>`;
    li.addEventListener("click", () => selectSubject(subject.id));
    subjectListEl.appendChild(li);
  }
  return subjects;
}

async function selectSubject(id) {
  activeSubjectId = id;
  emptyStateEl.hidden = true;
  subjectViewEl.hidden = false;
  await loadSubjects();
  await refreshSubjectDetail();
  restartPolling();
  renderChatHistory();
}

async function refreshSubjectDetail() {
  if (activeSubjectId === null) return;
  const subject = await api(`/subjects/${activeSubjectId}`);
  subjectTitleEl.textContent = subject.name;
  renderMaterials(subject.materials);
}

function renderMaterials(materials) {
  materialsBodyEl.innerHTML = "";
  for (const m of materials) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(m.filename)}</td>
      <td><span class="status status-${m.status}">${m.status}${m.status === "error" ? ": " + escapeHtml(m.error_message || "") : ""}</span></td>
      <td>${m.chunk_count ?? "-"}</td>
      <td>${new Date(m.uploaded_at).toLocaleString()}</td>
      <td><span class="delete-link" data-id="${m.id}">delete</span></td>
    `;
    tr.querySelector(".delete-link").addEventListener("click", async (e) => {
      const id = e.target.getAttribute("data-id");
      await api(`/materials/${id}`, { method: "DELETE" });
      await refreshSubjectDetail();
      await loadSubjects();
    });
    materialsBodyEl.appendChild(tr);
  }
}

function restartPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (activeSubjectId === null) return;
    await refreshSubjectDetail();
  }, 3000);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

document.getElementById("new-subject-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("new-subject-name");
  const name = input.value.trim();
  if (!name) return;
  const subject = await api("/subjects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  input.value = "";
  await selectSubject(subject.id);
});

document.getElementById("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (activeSubjectId === null) return;
  const fileInput = document.getElementById("upload-input");
  const files = Array.from(fileInput.files);
  if (files.length === 0) return;

  const submitBtn = e.target.querySelector("button[type=submit]");
  submitBtn.disabled = true;
  submitBtn.textContent = "Uploading...";

  const progressListEl = document.getElementById("upload-progress-list");
  progressListEl.innerHTML = "";

  const rows = files.map((file) => {
    const row = document.createElement("div");
    row.className = "upload-progress-item";
    row.innerHTML = `
      <div class="filename-row">
        <span class="filename">${escapeHtml(file.name)}</span>
        <span class="pct">0%</span>
      </div>
      <div class="upload-progress-bar-track">
        <div class="upload-progress-bar-fill"></div>
      </div>
    `;
    progressListEl.appendChild(row);
    return row;
  });

  const uploads = files.map((file, i) => {
    const row = rows[i];
    const fill = row.querySelector(".upload-progress-bar-fill");
    const pct = row.querySelector(".pct");

    return uploadFileWithProgress(`/subjects/${activeSubjectId}/materials`, file, (percent) => {
      fill.style.width = percent + "%";
      pct.textContent = percent + "%";
    })
      .then(() => {
        row.classList.add("done");
        fill.style.width = "100%";
        pct.textContent = "Uploaded";
        refreshSubjectDetail();
        loadSubjects();
      })
      .catch((err) => {
        row.classList.add("failed");
        pct.textContent = "Failed";
        row.insertAdjacentHTML("beforeend", `<div class="error-msg">${escapeHtml(err.message)}</div>`);
      });
  });

  await Promise.allSettled(uploads);

  fileInput.value = "";
  submitBtn.disabled = false;
  submitBtn.textContent = "Upload material";
  await refreshSubjectDetail();
  await loadSubjects();

  setTimeout(() => {
    progressListEl.innerHTML = "";
  }, 4000);
});

function renderChatHistory() {
  const history = chatHistoryBySubject[activeSubjectId] || [];
  chatMessagesEl.innerHTML = "";

  if (history.length === 0) {
    chatMessagesEl.innerHTML =
      '<div class="chat-empty">Ask a question about this subject\'s material to get started.</div>';
    return;
  }

  for (const turn of history) {
    appendChatBubble(turn.role, turn.content, turn.sources);
  }
  chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
}

function appendChatBubble(role, content, sources) {
  if (chatMessagesEl.querySelector(".chat-empty")) {
    chatMessagesEl.innerHTML = "";
  }

  const bubble = document.createElement("div");
  bubble.className = `chat-bubble ${role}`;
  bubble.textContent = content;
  chatMessagesEl.appendChild(bubble);

  if (sources && sources.length > 0) {
    const sourcesEl = document.createElement("div");
    sourcesEl.className = "chat-sources";
    const names = [...new Set(sources.map((s) => s.filename))];
    sourcesEl.textContent = "Sources: " + names.join(", ");
    chatMessagesEl.appendChild(sourcesEl);
  }

  chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
  return bubble;
}

document.getElementById("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (activeSubjectId === null) return;

  const input = document.getElementById("chat-input");
  const message = input.value.trim();
  if (!message) return;

  const subjectId = activeSubjectId;
  const history = (chatHistoryBySubject[subjectId] ||= []);

  history.push({ role: "user", content: message });
  appendChatBubble("user", message);
  input.value = "";

  const pendingBubble = appendChatBubble("assistant pending", "Thinking...");

  try {
    const response = await api(`/subjects/${subjectId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        history: history.slice(0, -1).map((h) => ({ role: h.role, content: h.content })),
      }),
    });

    pendingBubble.remove();
    history.push({ role: "assistant", content: response.answer, sources: response.sources });
    appendChatBubble("assistant", response.answer, response.sources);
  } catch (err) {
    pendingBubble.remove();
    appendChatBubble("assistant error", err.message);
  }
});

loadSubjects();

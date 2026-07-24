const API = "/api";

let activeSubjectId = null;
let pollTimer = null;

const subjectListEl = document.getElementById("subject-list");
const emptyStateEl = document.getElementById("empty-state");
const subjectViewEl = document.getElementById("subject-view");
const subjectTitleEl = document.getElementById("subject-title");
const materialsBodyEl = document.getElementById("materials-body");

async function api(path, options) {
  const res = await fetch(API + path, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed: ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
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

  const results = await Promise.allSettled(
    files.map((file) => {
      const formData = new FormData();
      formData.append("file", file);
      return api(`/subjects/${activeSubjectId}/materials`, {
        method: "POST",
        body: formData,
      });
    })
  );

  fileInput.value = "";
  await refreshSubjectDetail();
  await loadSubjects();

  const failures = results.filter((r) => r.status === "rejected");
  if (failures.length > 0) {
    alert(
      `${failures.length} of ${files.length} file(s) failed to upload:\n` +
        failures.map((r) => r.reason.message).join("\n")
    );
  }
});

loadSubjects();

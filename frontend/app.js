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

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), ...(await Auth.authHeaders()) };
  const res = await fetch(API + path, { ...options, headers });
  if (res.status === 401) {
    // The session expired or was revoked. Drop back to the sign-in
    // screen rather than showing a wall of failures.
    Auth.signOut();
    showSignIn();
    throw new Error("Please sign in again");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed: ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

// A browser cannot put an Authorization header on <img src>, so figures
// are fetched like any other request and handed to the tag as a blob.
const blobUrlCache = new Map();

async function authedImage(imgEl, url) {
  if (blobUrlCache.has(url)) {
    imgEl.src = blobUrlCache.get(url);
    return;
  }
  try {
    const res = await fetch(API + url.replace(/^\/api/, ""), {
      headers: await Auth.authHeaders(),
    });
    if (!res.ok) return;
    const objectUrl = URL.createObjectURL(await res.blob());
    blobUrlCache.set(url, objectUrl);
    imgEl.src = objectUrl;
  } catch (_) {
    // A figure that will not load is not worth breaking the answer over.
  }
}

// fetch() can't report upload progress, so file uploads use XHR instead.
async function uploadFileWithProgress(path, file, onProgress) {
  const headers = await Auth.authHeaders();
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", API + path);
    for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v);
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
    const scope = subject.classroom_name
      ? `<span class="subject-scope">${escapeHtml(subject.classroom_name)}</span>`
      : "";
    li.innerHTML =
      `<span class="subject-name">${escapeHtml(subject.name)}${scope}</span>` +
      `<span class="count">${subject.material_count}</span>`;
    li.addEventListener("click", () => selectSubject(subject.id));
    subjectListEl.appendChild(li);
  }
  return subjects;
}

async function selectSubject(id) {
  activeSubjectId = id;
  emptyStateEl.hidden = true;
  subjectViewEl.hidden = false;
  // A test belongs to the subject it was generated for, so switching
  // subject abandons anything in progress rather than carrying it over.
  stopTimer();
  activeTest = null;
  activeAttemptId = null;
  showTestView("setup");
  await loadSubjects();
  await refreshSubjectDetail();
  restartPolling();
  renderChatHistory();
  refreshTestPanel();
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
    // A file can index successfully and still have scanned pages whose
    // content never made it in. That is invisible from the chunk count
    // alone, so call it out on the row.
    const scanned = m.scanned_page_count || 0;
    const recognised = m.ocr_page_count || 0;
    let scanNote = "";
    if (scanned > 0 && m.status === "processed") {
      scanNote =
        recognised > 0
          ? `<span class="scan-note" title="These pages had no text layer, so their text was read from the page image. Unreadable handwriting is marked [illegible] rather than guessed.">${recognised} of ${scanned} scanned pages read by OCR</span>`
          : `<span class="scan-warning" title="These pages are images with no text layer, so their content is not searchable.">${scanned} of ${m.page_count} pages scanned &mdash; not indexed</span>`;
    }

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(m.filename)}</td>
      <td><span class="status status-${m.status}">${m.status}${m.status === "error" ? ": " + escapeHtml(m.error_message || "") : ""}</span>${scanNote}</td>
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
  const target = document.getElementById("new-subject-target").value;
  const body = { name };
  if (target === "shared") body.shared = true;
  else if (target.startsWith("class:")) body.classroom_id = Number(target.slice(6));

  const subject = await api("/subjects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
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
    appendChatBubble(turn.role, turn.content, turn.sources, turn.images);
  }
  chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
}

function appendChatBubble(role, content, sources, images) {
  if (chatMessagesEl.querySelector(".chat-empty")) {
    chatMessagesEl.innerHTML = "";
  }

  const bubble = document.createElement("div");
  bubble.className = `chat-bubble ${role}`;
  bubble.textContent = content;
  chatMessagesEl.appendChild(bubble);

  if (images && images.length > 0) {
    const figuresEl = document.createElement("div");
    figuresEl.className = "chat-figures";
    for (const img of images) {
      const figure = document.createElement("figure");
      figure.className = "chat-figure";

      const el = document.createElement("img");
      authedImage(el, img.url);
      el.alt = `Figure from ${img.filename}, page ${img.page}`;
      el.loading = "lazy";
      el.addEventListener("click", () => openLightbox(img));
      figure.appendChild(el);

      const caption = document.createElement("figcaption");
      if (img.caption) {
        const label = document.createElement("span");
        label.className = "figure-caption-text";
        label.textContent = img.caption;
        caption.appendChild(label);
      }
      const origin = document.createElement("span");
      origin.className = "figure-origin";
      origin.textContent = `${img.filename} · page ${img.page}`;
      caption.appendChild(origin);
      figure.appendChild(caption);

      figuresEl.appendChild(figure);
    }
    chatMessagesEl.appendChild(figuresEl);
  }

  if (sources && sources.length > 0) {
    const sourcesEl = document.createElement("div");
    sourcesEl.className = "chat-sources";
    for (const s of sources) {
      const details = document.createElement("details");
      details.className = "chat-source-item";

      const summary = document.createElement("summary");
      const where = s.page ? `${s.filename} · p.${s.page}` : s.filename;
      summary.textContent = `${where} · ${Math.round(s.score * 100)}% match`;
      details.appendChild(summary);

      const textEl = document.createElement("div");
      textEl.className = "chat-source-text";
      textEl.textContent = s.text;
      details.appendChild(textEl);

      sourcesEl.appendChild(details);
    }
    chatMessagesEl.appendChild(sourcesEl);
  }

  chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
  return bubble;
}

function openLightbox(img) {
  const overlay = document.createElement("div");
  overlay.className = "lightbox";
  overlay.innerHTML = `
    <img alt="Figure from ${escapeHtml(img.filename)}, page ${img.page}" />
    <div class="lightbox-caption">${escapeHtml(img.filename)} &middot; page ${img.page}</div>
  `;
  // Cached from the thumbnail, so this is normally instant.
  authedImage(overlay.querySelector("img"), img.url);
  overlay.addEventListener("click", () => overlay.remove());
  document.addEventListener(
    "keydown",
    function onKey(e) {
      if (e.key === "Escape") {
        overlay.remove();
        document.removeEventListener("keydown", onKey);
      }
    }
  );
  document.body.appendChild(overlay);
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
    history.push({
      role: "assistant",
      content: response.answer,
      sources: response.sources,
      images: response.images,
    });
    appendChatBubble("assistant", response.answer, response.sources, response.images);
  } catch (err) {
    pendingBubble.remove();
    appendChatBubble("assistant error", err.message);
  }
});

// ---------------------------------------------------------------- Tests

let activeTest = null;
let activeAttemptId = null;
let timerInterval = null;

const testSetupViewEl = document.getElementById("test-setup-view");
const testTakeViewEl = document.getElementById("test-take-view");
const testResultViewEl = document.getElementById("test-result-view");
const testMaterialListEl = document.getElementById("test-material-list");
const testListEl = document.getElementById("test-list");
const testQuestionsEl = document.getElementById("test-questions");
const testResultsEl = document.getElementById("test-results");
const testTimerEl = document.getElementById("test-timer");

const KIND_LABELS = { mcq: "Multiple choice", short: "Short answer", long: "Long answer" };

function showTestView(which) {
  testSetupViewEl.hidden = which !== "setup";
  testTakeViewEl.hidden = which !== "take";
  testResultViewEl.hidden = which !== "result";
}

function showError(el, message) {
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = message;
}

// The material checkboxes double as the "is a test possible yet" signal:
// only processed materials have chunks to draw questions from.
async function renderTestMaterials() {
  const subject = await api(`/subjects/${activeSubjectId}`);
  const ready = subject.materials.filter((m) => m.status === "processed");
  testMaterialListEl.innerHTML = "";

  if (ready.length === 0) {
    testMaterialListEl.innerHTML =
      '<p class="hint">No processed materials yet. Upload material and wait for it to finish processing.</p>';
    return;
  }

  for (const m of ready) {
    const label = document.createElement("label");
    label.className = "test-material-item";
    label.innerHTML = `
      <input type="checkbox" value="${m.id}" checked />
      <span class="test-material-name">${escapeHtml(m.filename)}</span>
      <span class="test-material-meta">${m.chunk_count ?? "-"} chunks</span>
    `;
    testMaterialListEl.appendChild(label);
  }
}

async function renderTestList() {
  const tests = await api(`/subjects/${activeSubjectId}/tests`);
  testListEl.innerHTML = "";

  if (tests.length === 0) {
    testListEl.innerHTML = '<p class="hint">No tests yet. Generate one above.</p>';
    return;
  }

  for (const t of tests) {
    const row = document.createElement("div");
    row.className = "test-list-item";
    const best =
      t.attempt_count > 0 && t.best_score !== null
        ? `Best ${Math.round((t.best_score / t.max_points) * 100)}% · ${t.attempt_count} attempt${t.attempt_count === 1 ? "" : "s"}`
        : "Not attempted";
    row.innerHTML = `
      <div class="test-list-main">
        <span class="test-list-title">${escapeHtml(t.title)}</span>
        <span class="test-list-meta">
          ${t.question_count} questions · ${t.max_points} marks${t.time_limit_minutes ? ` · ${t.time_limit_minutes} min` : ""} · ${escapeHtml(best)}
        </span>
      </div>
      <div class="test-list-actions">
        <button type="button" class="start-test">Take test</button>
        <span class="delete-link">delete</span>
      </div>
    `;
    row.querySelector(".start-test").addEventListener("click", () => startTest(t.id));
    row.querySelector(".delete-link").addEventListener("click", async () => {
      await api(`/tests/${t.id}`, { method: "DELETE" });
      await renderTestList();
    });
    testListEl.appendChild(row);
  }
}

async function refreshTestPanel() {
  if (activeSubjectId === null) return;
  await Promise.all([renderTestMaterials(), renderTestList()]);
}

function stopTimer() {
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = null;
  testTimerEl.hidden = true;
}

function startTimer(minutes) {
  stopTimer();
  if (!minutes) return;
  let remaining = minutes * 60;
  testTimerEl.hidden = false;

  const tick = () => {
    const m = Math.floor(remaining / 60);
    const s = String(remaining % 60).padStart(2, "0");
    testTimerEl.textContent = `${m}:${s}`;
    testTimerEl.classList.toggle("urgent", remaining <= 60);
    if (remaining <= 0) {
      stopTimer();
      // Time up submits what the student has so far rather than
      // discarding it - a blank answer still gets feedback.
      document.getElementById("test-take-form").requestSubmit();
      return;
    }
    remaining -= 1;
  };
  tick();
  timerInterval = setInterval(tick, 1000);
}

async function startTest(testId) {
  activeTest = await api(`/tests/${testId}`);
  const attempt = await api(`/tests/${testId}/attempts`, { method: "POST" });
  activeAttemptId = attempt.id;

  document.getElementById("test-take-title").textContent = activeTest.title;
  document.getElementById("test-take-meta").textContent =
    `${activeTest.question_count} questions · ${activeTest.max_points} marks` +
    (activeTest.time_limit_minutes ? ` · ${activeTest.time_limit_minutes} minute limit` : "");
  showError(document.getElementById("test-take-error"), null);

  testQuestionsEl.innerHTML = "";
  for (const q of activeTest.questions) {
    const block = document.createElement("div");
    block.className = "test-question";
    block.dataset.questionId = q.id;

    const header = `
      <div class="test-question-header">
        <span class="test-question-number">${q.position + 1}</span>
        <span class="test-question-kind">${KIND_LABELS[q.kind] || q.kind}</span>
        <span class="test-question-points">${q.points} ${q.points === 1 ? "mark" : "marks"}</span>
      </div>
      <p class="test-question-prompt">${escapeHtml(q.prompt)}</p>
    `;

    let body = "";
    if (q.kind === "mcq" && q.options) {
      body = q.options
        .map(
          (opt, i) => `
        <label class="test-option">
          <input type="radio" name="q${q.id}" value="${i}" />
          <span>${escapeHtml(opt)}</span>
        </label>`
        )
        .join("");
      body = `<div class="test-options-list">${body}</div>`;
    } else {
      const rows = q.kind === "long" ? 7 : 3;
      body = `<textarea name="q${q.id}" rows="${rows}" placeholder="Your answer..."></textarea>`;
    }

    block.innerHTML = header + body;
    testQuestionsEl.appendChild(block);
  }

  showTestView("take");
  startTimer(activeTest.time_limit_minutes);
}

function collectAnswers() {
  return activeTest.questions.map((q) => {
    if (q.kind === "mcq") {
      const picked = testQuestionsEl.querySelector(`input[name="q${q.id}"]:checked`);
      return {
        question_id: q.id,
        selected_option: picked ? Number(picked.value) : null,
      };
    }
    const field = testQuestionsEl.querySelector(`[name="q${q.id}"]`);
    return { question_id: q.id, response: field ? field.value : "" };
  });
}

function renderResults(attempt) {
  const pct = attempt.max_points
    ? Math.round((attempt.score_points / attempt.max_points) * 100)
    : 0;
  document.getElementById("test-result-title").textContent = activeTest
    ? activeTest.title
    : "Results";
  document.getElementById("test-result-score").textContent =
    `${attempt.score_points} / ${attempt.max_points} marks · ${pct}%`;

  testResultsEl.innerHTML = "";
  for (const a of attempt.answers) {
    const block = document.createElement("div");
    // An unmarked answer is neither right nor wrong - keep it visually
    // distinct from a zero so it does not read as a wrong answer.
    const state = a.is_correct === null ? "unmarked" : a.is_correct ? "correct" : "incorrect";
    block.className = `test-result-item ${state}`;

    let yours = "";
    if (a.kind === "mcq" && a.options) {
      yours = a.options
        .map((opt, i) => {
          const marks = [];
          if (i === a.selected_option) marks.push("chosen");
          if (i === a.correct_option) marks.push("correct-option");
          return `<div class="test-result-option ${marks.join(" ")}">${escapeHtml(opt)}</div>`;
        })
        .join("");
    } else {
      yours = `<div class="test-result-response">${
        a.response ? escapeHtml(a.response) : "<em>Left blank</em>"
      }</div>`;
    }

    const awarded = a.awarded_points === null ? "—" : a.awarded_points;
    const source = a.source_filename
      ? `<p class="test-result-source">Source: ${escapeHtml(a.source_filename)}${a.source_page ? `, page ${a.source_page}` : ""}</p>`
      : "";

    block.innerHTML = `
      <div class="test-question-header">
        <span class="test-question-number">${a.position + 1}</span>
        <span class="test-question-kind">${KIND_LABELS[a.kind] || a.kind}</span>
        <span class="test-question-points">${awarded} / ${a.points}</span>
      </div>
      <p class="test-question-prompt">${escapeHtml(a.prompt)}</p>
      ${yours}
      ${a.feedback ? `<p class="test-result-feedback">${escapeHtml(a.feedback)}</p>` : ""}
      ${
        a.expected_answer
          ? `<details class="test-result-expected"><summary>Model answer</summary><div>${escapeHtml(a.expected_answer)}</div></details>`
          : ""
      }
      ${a.explanation ? `<p class="test-result-explanation">${escapeHtml(a.explanation)}</p>` : ""}
      ${source}
    `;
    testResultsEl.appendChild(block);
  }

  showTestView("result");
}

document.getElementById("test-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (activeSubjectId === null) return;

  const errorEl = document.getElementById("test-form-error");
  showError(errorEl, null);

  const picked = Array.from(
    testMaterialListEl.querySelectorAll("input[type=checkbox]:checked")
  ).map((c) => Number(c.value));
  if (picked.length === 0) {
    showError(errorEl, "Pick at least one material for the test to draw on.");
    return;
  }

  const button = e.target.querySelector("button[type=submit]");
  button.disabled = true;
  button.textContent = "Writing questions...";

  try {
    const timeLimit = document.getElementById("test-time-limit").value;
    await api(`/subjects/${activeSubjectId}/tests`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        material_ids: picked,
        question_count: Number(document.getElementById("test-question-count").value),
        time_limit_minutes: timeLimit ? Number(timeLimit) : null,
      }),
    });
    await renderTestList();
  } catch (err) {
    showError(errorEl, err.message);
  } finally {
    button.disabled = false;
    button.textContent = "Generate test";
  }
});

document.getElementById("test-take-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (activeAttemptId === null) return;

  const errorEl = document.getElementById("test-take-error");
  showError(errorEl, null);
  const button = document.getElementById("test-submit");
  button.disabled = true;
  button.textContent = "Marking...";

  try {
    const attempt = await api(`/attempts/${activeAttemptId}/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers: collectAnswers() }),
    });
    stopTimer();
    renderResults(attempt);
    activeAttemptId = null;
    renderTestList();
  } catch (err) {
    showError(errorEl, err.message);
  } finally {
    button.disabled = false;
    button.textContent = "Submit answers";
  }
});

document.getElementById("test-abandon").addEventListener("click", () => {
  stopTimer();
  activeAttemptId = null;
  showTestView("setup");
  refreshTestPanel();
});

document.getElementById("test-result-back").addEventListener("click", () => {
  showTestView("setup");
  refreshTestPanel();
});

const tabButtons = document.querySelectorAll(".tab-button");
const tabPanels = {
  chat: document.getElementById("chat-panel"),
  tests: document.getElementById("tests-panel"),
  materials: document.getElementById("materials-panel"),
};

for (const button of tabButtons) {
  button.addEventListener("click", () => {
    for (const b of tabButtons) b.classList.remove("active");
    button.classList.add("active");
    for (const [name, panel] of Object.entries(tabPanels)) {
      panel.hidden = name !== button.dataset.tab;
    }
    // Materials may have finished processing since the tab was last
    // opened, which changes what a test can be built from. Skip it while
    // a test is in progress, so switching tabs mid-test does not wipe
    // the student's answers.
    if (button.dataset.tab === "tests" && !testSetupViewEl.hidden) {
      refreshTestPanel();
    }
  });
}

// ------------------------------------------------------- Sign-in / profile

const signInScreenEl = document.getElementById("signin-screen");
const appLayoutEl = document.getElementById("app-layout");
const profileViewEl = document.getElementById("profile-view");
let signUpMode = false;
let me = null;

function showSignIn() {
  signInScreenEl.hidden = false;
  appLayoutEl.hidden = true;
  if (pollTimer) clearInterval(pollTimer);
}

function showApp() {
  signInScreenEl.hidden = true;
  appLayoutEl.hidden = false;
}

function showProfile(open) {
  profileViewEl.hidden = !open;
  classesViewEl.hidden = true;
  subjectViewEl.hidden = open || activeSubjectId === null;
  emptyStateEl.hidden = open || activeSubjectId !== null;
}

function fillProfileForm(profile) {
  const form = document.getElementById("profile-form");
  for (const field of form.querySelectorAll("input[name], textarea[name]")) {
    field.value = (profile && profile[field.name]) || "";
  }
}

async function loadMe() {
  me = await api("/me");
  document.getElementById("account-school").textContent = me.school_name || "";
  document.getElementById("account-name").textContent =
    me.display_name || me.email || "Signed in";
  document.getElementById("account-role").hidden = !me.is_admin;
  fillProfileForm(me.profile);
  return me;
}

document.getElementById("signin-toggle").addEventListener("click", (e) => {
  e.preventDefault();
  signUpMode = !signUpMode;
  document.getElementById("signin-submit").textContent = signUpMode ? "Create account" : "Sign in";
  document.getElementById("signin-switch-text").textContent = signUpMode
    ? "Already have an account?"
    : "New here?";
  e.target.textContent = signUpMode ? "Sign in instead" : "Create an account";
  document.getElementById("signin-password").autocomplete = signUpMode
    ? "new-password"
    : "current-password";
  showError(document.getElementById("signin-error"), null);
});

document.getElementById("signin-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById("signin-error");
  const button = document.getElementById("signin-submit");
  const email = document.getElementById("signin-email").value.trim();
  const password = document.getElementById("signin-password").value;

  showError(errorEl, null);
  button.disabled = true;
  try {
    if (signUpMode) await Auth.signUp(email, password);
    else await Auth.signIn(email, password);
    document.getElementById("signin-password").value = "";
    await start();
  } catch (err) {
    showError(errorEl, err.message);
  } finally {
    button.disabled = false;
  }
});

document.getElementById("sign-out").addEventListener("click", () => {
  Auth.signOut();
  onboardScreenEl.hidden = true;
  activeSubjectId = null;
  me = null;
  // Blob URLs belong to the signed-out session; do not leave another
  // account looking at the previous one's figures.
  for (const url of blobUrlCache.values()) URL.revokeObjectURL(url);
  blobUrlCache.clear();
  showSignIn();
});

document.getElementById("open-profile").addEventListener("click", () => showProfile(true));
document.getElementById("close-profile").addEventListener("click", () => showProfile(false));

document.getElementById("profile-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById("profile-error");
  const savedEl = document.getElementById("profile-saved");
  const button = document.getElementById("profile-save");
  showError(errorEl, null);
  savedEl.hidden = true;
  button.disabled = true;

  const payload = {};
  for (const field of e.target.querySelectorAll("input[name], textarea[name]")) {
    payload[field.name] = field.value;
  }

  try {
    const profile = await api("/me/profile", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    fillProfileForm(profile);
    savedEl.hidden = false;
    setTimeout(() => (savedEl.hidden = true), 2500);
  } catch (err) {
    showError(errorEl, err.message);
  } finally {
    button.disabled = false;
  }
});

// --------------------------------------------------------------- Classes

const classesViewEl = document.getElementById("classes-view");
let myClasses = [];

function showClasses(open) {
  classesViewEl.hidden = !open;
  profileViewEl.hidden = true;
  subjectViewEl.hidden = open || activeSubjectId === null;
  emptyStateEl.hidden = open || activeSubjectId !== null;
  if (open) renderClasses();
}

// Where a new subject goes. A teacher can put one in a class they run; an
// administrator can add to the shared library; otherwise it is personal.
function renderSubjectTarget() {
  const select = document.getElementById("new-subject-target");
  const options = [`<option value="">Just for me</option>`];
  if (me && me.is_admin) options.push(`<option value="shared">Shared library</option>`);
  for (const c of myClasses.filter((c) => c.teaching)) {
    options.push(`<option value="class:${c.id}">${escapeHtml(c.name)}</option>`);
  }
  select.innerHTML = options.join("");
  select.hidden = options.length < 2;
}

async function loadClasses() {
  myClasses = await api("/classes");
  renderSubjectTarget();
  return myClasses;
}

function renderClassCard(c) {
  const row = document.createElement("div");
  row.className = "test-list-item class-card";
  row.innerHTML = `
    <div class="test-list-main">
      <span class="test-list-title">${escapeHtml(c.name)}</span>
      <span class="test-list-meta">
        ${c.teaching ? "You teach this" : "Taught by " + escapeHtml(c.teacher_name || "—")}
        · ${c.subject_count} subject${c.subject_count === 1 ? "" : "s"}${
          c.teaching ? ` · ${c.member_count} student${c.member_count === 1 ? "" : "s"}` : ""
        }
      </span>
    </div>
    ${c.teaching ? `<div class="test-list-actions">
      <button type="button" class="manage-class">Manage</button>
    </div>` : ""}
  `;
  if (c.teaching) {
    row.querySelector(".manage-class").addEventListener("click", () => openClassDetail(c));
  }
  return row;
}

async function renderClasses() {
  const listEl = document.getElementById("class-list");
  const hintEl = document.getElementById("classes-hint");
  const formEl = document.getElementById("class-form");

  const canTeach = me && (me.is_teacher || me.is_admin);
  formEl.hidden = !canTeach;
  hintEl.textContent = canTeach
    ? "Material you add to a class is visible to everyone on its roster. Students can still upload their own files, which stay private to them."
    : "Classes you have been added to. Their material appears alongside your own subjects.";

  document.getElementById("admin-section").hidden = !(me && me.is_admin);
  if (me && me.is_admin) renderAdminUsers();

  await loadClasses();
  listEl.innerHTML = "";
  if (myClasses.length === 0) {
    listEl.innerHTML = `<p class="hint">${
      canTeach ? "No classes yet. Create one above." : "You are not in any class yet."
    }</p>`;
    return;
  }
  for (const c of myClasses) listEl.appendChild(renderClassCard(c));
}

async function openClassDetail(c) {
  const listEl = document.getElementById("class-list");
  const [members, progress] = await Promise.all([
    api(`/classes/${c.id}/members`),
    api(`/classes/${c.id}/progress`),
  ]);

  const scores = {};
  for (const s of progress.students) scores[s.email || s.name] = s;

  listEl.innerHTML = "";
  const panel = document.createElement("div");
  panel.className = "class-detail";
  panel.innerHTML = `
    <div class="test-take-header">
      <div><h3>${escapeHtml(c.name)}</h3>
        <p class="hint">A student is a person on the roll, not an account. Add them with
        or without an email; if you give one, their login connects to this record the
        first time they sign in.</p></div>
      <div class="test-take-actions">
        <button type="button" class="secondary-button take-attendance">Attendance</button>
        <button type="button" class="secondary-button back-to-classes">Back</button>
      </div>
    </div>
    <form class="add-member">
      <input name="full_name" type="text" placeholder="Student name" />
      <input name="email" type="email" placeholder="Email (optional)" />
      <input name="roll_number" type="text" placeholder="Roll no." class="roll-input" />
      <button type="submit">Add student</button>
    </form>
    <table class="materials-table">
      <thead><tr><th>Roll</th><th>Student</th><th>Account</th><th>Tests taken</th><th>Average</th><th></th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="member-error test-error" hidden></div>
  `;

  const tbody = panel.querySelector("tbody");
  for (const m of members) {
    const s = scores[m.email || m.full_name] || {};
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(m.roll_number || "—")}</td>
      <td>${escapeHtml(m.full_name)}${
        m.email ? `<br><span class="test-list-meta">${escapeHtml(m.email)}</span>` : ""
      }</td>
      <td><span class="status ${m.has_account ? "status-processed" : "status-uploaded"}" title="${
        m.has_account ? "This student has signed in." : "Enrolled. They can sign in with this address whenever they like."
      }">${m.has_account ? "signed in" : "no account"}</span></td>
      <td>${(s.attempts || []).length}</td>
      <td>${s.average_percent !== null && s.average_percent !== undefined ? s.average_percent + "%" : "—"}</td>
      <td><span class="delete-link">remove</span></td>
    `;
    tr.querySelector(".delete-link").addEventListener("click", async () => {
      await api(`/classes/${c.id}/members/${m.id}`, { method: "DELETE" });
      openClassDetail(c);
    });
    tbody.appendChild(tr);
  }

  panel.querySelector(".back-to-classes").addEventListener("click", renderClasses);
  panel.querySelector(".take-attendance").addEventListener("click", () => openAttendance(c));
  panel.querySelector(".add-member").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = panel.querySelector(".member-error");
    showError(errorEl, null);
    const body = {};
    for (const field of e.target.querySelectorAll("input[name]")) {
      body[field.name] = field.value.trim();
    }
    try {
      await api(`/classes/${c.id}/members`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      openClassDetail(c);
    } catch (err) {
      showError(errorEl, err.message);
    }
  });

  listEl.appendChild(panel);
}

// The register is a daily job, so it opens with everyone present and the
// teacher only marks the exceptions - the common case is no typing at all.
async function openAttendance(c, day) {
  const listEl = document.getElementById("class-list");
  const on = day || new Date().toISOString().slice(0, 10);
  const data = await api(`/classes/${c.id}/attendance?on=${on}`);

  listEl.innerHTML = "";
  const panel = document.createElement("div");
  panel.className = "class-detail";
  panel.innerHTML = `
    <div class="test-take-header">
      <div>
        <h3>${escapeHtml(c.name)} &mdash; attendance</h3>
        <p class="hint" id="att-state"></p>
      </div>
      <div class="test-take-actions">
        <input type="date" class="att-date" value="${on}" />
        <button type="button" class="secondary-button back-to-class">Back</button>
      </div>
    </div>
    <div class="att-list"></div>
    <div class="att-actions">
      <button type="button" class="att-save">Save register</button>
      <span class="att-saved profile-saved" hidden>Saved</span>
      <span class="att-summary-link delete-link">View summary</span>
    </div>
    <div class="att-error test-error" hidden></div>
  `;

  panel.querySelector("#att-state").textContent = data.taken
    ? `Marked ${new Date(data.taken_at).toLocaleString()} — changing it here corrects the record.`
    : "Not marked yet. Everyone starts present; tap a student to change them.";

  const listWrap = panel.querySelector(".att-list");
  if (data.entries.length === 0) {
    listWrap.innerHTML = '<p class="hint">No students enrolled in this class yet.</p>';
  }

  const state = new Map();
  for (const e of data.entries) {
    state.set(e.enrolment_id, e.status || "present");
    const row = document.createElement("div");
    row.className = "att-row";
    row.innerHTML = `
      <span class="att-roll">${escapeHtml(e.roll_number || "—")}</span>
      <span class="att-name">${escapeHtml(e.full_name)}</span>
      <span class="att-buttons">
        ${["present", "absent", "late", "excused"]
          .map(
            (st) =>
              `<button type="button" class="att-btn att-${st}" data-status="${st}">${st}</button>`
          )
          .join("")}
      </span>
    `;
    const paint = () => {
      const current = state.get(e.enrolment_id);
      for (const b of row.querySelectorAll(".att-btn")) {
        b.classList.toggle("active", b.dataset.status === current);
      }
    };
    for (const b of row.querySelectorAll(".att-btn")) {
      b.addEventListener("click", () => {
        state.set(e.enrolment_id, b.dataset.status);
        paint();
      });
    }
    paint();
    listWrap.appendChild(row);
  }

  panel.querySelector(".back-to-class").addEventListener("click", () => openClassDetail(c));
  panel.querySelector(".att-date").addEventListener("change", (e) =>
    openAttendance(c, e.target.value)
  );
  panel.querySelector(".att-summary-link").addEventListener("click", () =>
    openAttendanceSummary(c)
  );

  panel.querySelector(".att-save").addEventListener("click", async (e) => {
    const errorEl = panel.querySelector(".att-error");
    const savedEl = panel.querySelector(".att-saved");
    showError(errorEl, null);
    e.target.disabled = true;
    try {
      await api(`/classes/${c.id}/attendance?on=${panel.querySelector(".att-date").value}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          entries: [...state].map(([enrolment_id, status]) => ({ enrolment_id, status })),
        }),
      });
      savedEl.hidden = false;
      setTimeout(() => (savedEl.hidden = true), 2000);
      panel.querySelector("#att-state").textContent =
        "Marked just now — changing it here corrects the record.";
    } catch (err) {
      showError(errorEl, err.message);
    } finally {
      e.target.disabled = false;
    }
  });

  listEl.appendChild(panel);
}

async function openAttendanceSummary(c) {
  const listEl = document.getElementById("class-list");
  const data = await api(`/classes/${c.id}/attendance/summary`);
  listEl.innerHTML = "";
  const panel = document.createElement("div");
  panel.className = "class-detail";
  panel.innerHTML = `
    <div class="test-take-header">
      <div><h3>${escapeHtml(c.name)} &mdash; attendance summary</h3>
        <p class="hint">${data.sessions_held} session(s) recorded. Late counts as
        attended; excused is left out of the percentage entirely.</p></div>
      <button type="button" class="secondary-button back-to-att">Back</button>
    </div>
    <table class="materials-table">
      <thead><tr><th>Roll</th><th>Student</th><th>Present</th><th>Absent</th><th>Late</th><th>Excused</th><th>%</th></tr></thead>
      <tbody></tbody>
    </table>
  `;
  const tbody = panel.querySelector("tbody");
  for (const st of data.students) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(st.roll_number || "—")}</td>
      <td>${escapeHtml(st.full_name)}</td>
      <td>${st.present}</td><td>${st.absent}</td><td>${st.late}</td><td>${st.excused}</td>
      <td>${st.percent === null ? "—" : st.percent + "%"}</td>
    `;
    tbody.appendChild(tr);
  }
  panel.querySelector(".back-to-att").addEventListener("click", () => openAttendance(c));
  listEl.appendChild(panel);
}

async function renderAdminUsers() {
  const el = document.getElementById("admin-users");
  const users = await api("/admin/users");
  el.innerHTML = "";
  for (const u of users) {
    const row = document.createElement("div");
    row.className = "test-list-item";
    row.innerHTML = `
      <div class="test-list-main">
        <span class="test-list-title">${escapeHtml(u.display_name || u.email || "—")}</span>
        <span class="test-list-meta">${escapeHtml(u.email || "")}${u.is_admin ? " · admin" : ""}</span>
      </div>
      <div class="test-list-actions">
        <button type="button" class="secondary-button toggle-teacher">
          ${u.is_teacher ? "Remove teacher" : "Make teacher"}
        </button>
      </div>`;
    row.querySelector(".toggle-teacher").addEventListener("click", async () => {
      await api(`/admin/users/${u.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_teacher: !u.is_teacher }),
      });
      renderAdminUsers();
    });
    el.appendChild(row);
  }
}

document.getElementById("open-classes").addEventListener("click", () => showClasses(true));
document.getElementById("close-classes").addEventListener("click", () => showClasses(false));

document.getElementById("class-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("class-name");
  const errorEl = document.getElementById("classes-error");
  showError(errorEl, null);
  if (!input.value.trim()) return;
  try {
    await api("/classes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: input.value.trim() }),
    });
    input.value = "";
    await renderClasses();
  } catch (err) {
    showError(errorEl, err.message);
  }
});

const onboardScreenEl = document.getElementById("onboard-screen");

function showOnboard() {
  signInScreenEl.hidden = true;
  onboardScreenEl.hidden = false;
  appLayoutEl.hidden = true;
  if (pollTimer) clearInterval(pollTimer);
}

document.getElementById("onboard-recheck").addEventListener("click", async (e) => {
  e.preventDefault();
  await start();
});

document.getElementById("onboard-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById("onboard-error");
  const button = document.getElementById("onboard-submit");
  showError(errorEl, null);
  button.disabled = true;
  try {
    await api("/school", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: document.getElementById("onboard-name").value.trim() }),
    });
    await start();
  } catch (err) {
    showError(errorEl, err.message);
  } finally {
    button.disabled = false;
  }
});

async function start() {
  // Nothing exists outside a school, so an account without one is sent to
  // create or wait for an invitation rather than into an empty app.
  me = await api("/me");
  if (!me.school_id) {
    showOnboard();
    return;
  }
  onboardScreenEl.hidden = true;
  showApp();
  await loadMe();
  await loadClasses();
  await loadSubjects();
}

(async () => {
  await Auth.init();
  if (Auth.signedIn()) {
    try {
      await start();
      return;
    } catch (_) {
      // A stored session that no longer works lands here; fall through
      // to the sign-in screen rather than an empty app.
    }
  }
  showSignIn();
})();

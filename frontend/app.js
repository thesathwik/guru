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
      el.src = img.url;
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
    <img src="${img.url}" alt="Figure from ${escapeHtml(img.filename)}, page ${img.page}" />
    <div class="lightbox-caption">${escapeHtml(img.filename)} &middot; page ${img.page}</div>
  `;
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

loadSubjects();

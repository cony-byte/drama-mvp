// API_BASE: 서버가 다른 origin에 있을 때(GitHub Pages/Vercel + 로컬 터널) 쓸 주소.
// localStorage에 저장해두면 재배포 없이 터널 URL이 바뀔 때마다 UI에서 바로 바꿀 수 있다.
const API_BASE_KEY = "drama_mvp_api_base";

function getApiBase() {
  return localStorage.getItem(API_BASE_KEY) || "";
}

function setApiBase(v) {
  localStorage.setItem(API_BASE_KEY, v.trim());
}

// 마지막으로 연 작품/화를 기억해뒀다가 새로고침해도 그 자리로 돌아간다.
// ★2026-07-22: "AI 생성으로 고친 대본은 저장이 안 된다"는 리포트의 실제 원인 — 서버 저장 자체는
// 정상이었지만, 새로고침하면 항상 첫 화면(아이디어 입력)으로 돌아가고 "내 작품" 목록엔 제목이
// 같은 데모 카드가 여러 장 있어서, 사용자가 방금 고친 그 프로젝트가 아니라 다른(옛) 카드를 다시
// 열어 "안 고쳐진 대본"을 보게 됐다 — 실제로는 되돌아간 게 아니라 다른 프로젝트를 연 것.
const LAST_OPEN_KEY = "drama_mvp_last_open";

function saveLastOpen(projectId, episodeNum) {
  try {
    if (!projectId) { localStorage.removeItem(LAST_OPEN_KEY); return; }
    localStorage.setItem(LAST_OPEN_KEY, JSON.stringify({ projectId, episodeNum: episodeNum ?? null }));
  } catch (e) { /* localStorage 불가 환경 — 조용히 무시 */ }
}

function loadLastOpen() {
  try {
    return JSON.parse(localStorage.getItem(LAST_OPEN_KEY) || "null");
  } catch (e) {
    return null;
  }
}

const $ = (id) => document.getElementById(id);

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderInlineMarkdown(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+?)\*/g, "<em>$1</em>");
}

function renderScriptMarkdown(text) {
  const lines = String(text || "(아직 없음)").split(/\r?\n/);
  const firstContent = lines.findIndex((line) => line.trim());

  return lines.map((line, index) => {
    const trimmed = line.trim();
    if (!trimmed) return '<div class="script-spacer" aria-hidden="true"></div>';
    if (/^-{3,}$/.test(trimmed)) return '<hr class="script-divider">';

    const heading = trimmed.match(/^#{1,3}\s+(.+)$/);
    const singleStarTitle = trimmed.match(/^\*([^*].*?)\*$/);
    const boldLine = trimmed.match(/^\*\*(.+?)\*\*$/);
    const emphasizedTitle = index === firstContent
      && (singleStarTitle || (boldLine && !/^\d+\./.test(boldLine[1].trim())));
    if (heading || emphasizedTitle) {
      return `<h4 class="script-title">${renderInlineMarkdown((heading || emphasizedTitle)[1])}</h4>`;
    }

    const sceneHeading = boldLine;
    if (sceneHeading) {
      return `<div class="script-scene-heading">${renderInlineMarkdown(sceneHeading[1])}</div>`;
    }
    return `<div class="script-line">${renderInlineMarkdown(line)}</div>`;
  }).join("");
}

// "AI 생성" 버튼 첫 클릭 = 의견 입력창(툴팁 박스)만 펼치고 대기, 이미 펼쳐진 상태에서 클릭(또는
// 툴팁 안 "생성" 버튼) = 그 값으로 진행. 툴팁 박스는 textarea를 감싼 `${id}Box` — 없으면(구
// 마크업 호환) textarea 자체를 박스로 취급한다.
function _noteBox(noteInputId) {
  return $(noteInputId + "Box") || $(noteInputId);
}

function revealNoteThenProceed(noteInputId) {
  const box = _noteBox(noteInputId);
  if (box.classList.contains("hidden")) {
    box.classList.remove("hidden");
    $(noteInputId).focus();
    return false;
  }
  return true;
}

// AI 생성 성공 후(또는 "취소" 클릭 시) 툴팁을 닫고 입력했던 의견을 지운다(다음에 열었을 때
// 이전 내용이 남아있지 않게).
function hideNote(noteInputId) {
  $(noteInputId).value = "";
  _noteBox(noteInputId).classList.add("hidden");
}

// 툴팁 안 "취소"/"생성" 버튼 — 이벤트 위임(툴팁 4곳이 서로 다른 컨테이너에 있어 공통 리스너로).
document.addEventListener("click", (e) => {
  const cancelBtn = e.target.closest(".gen-note-cancel-btn");
  if (cancelBtn) {
    hideNote(cancelBtn.dataset.note);
    return;
  }
  const submitBtn = e.target.closest(".gen-note-submit-btn");
  if (submitBtn) {
    // 실제 생성 로직은 바깥 "🤖 AI 생성" 버튼 핸들러에 있다 — 툴팁이 이미 펼쳐진 상태이므로
    // revealNoteThenProceed가 즉시 true를 반환해 그대로 생성으로 진행된다(중복 구현 방지).
    $(submitBtn.dataset.trigger).click();
  }
});

const views = {
  input: $("inputView"),
  works: $("worksView"),
  chat: $("chatView"),
  pitch: $("pitchView"),
  videoPrep: $("videoPrepView"),
  studio: $("studioView"),
  characterDetail: $("characterDetailView"),
  episodeDetail: $("episodeDetailView"),
  stills: $("stillsView"),
  progress: $("progressView"),
  result: $("resultView"),
  error: $("errorView"),
};

function showView(name) {
  for (const v of Object.values(views)) v.classList.add("hidden");
  views[name].classList.remove("hidden");
}

let sessionId = null;

function addBubble(role, text) {
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.textContent = text;
  $("chatMessages").appendChild(div);
  $("chatMessages").scrollTop = $("chatMessages").scrollHeight;
  return div;
}

function renderOptions(options) {
  const box = $("chatOptions");
  box.innerHTML = "";
  if (!options || !options.length) {
    box.classList.add("hidden");
    return;
  }
  for (const label of options) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "option-chip";
    btn.textContent = label;
    btn.addEventListener("click", () => {
      $("chatInput").value = label;
      $("chatInput").focus();
    });
    box.appendChild(btn);
  }
  box.classList.remove("hidden");
}

async function startChat() {
  const idea = $("ideaInput").value.trim();
  if (!idea) return;
  const base = getApiBase();
  $("startChatBtn").disabled = true;
  try {
    showView("chat");
    $("chatMessages").innerHTML = "";
    renderOptions([]);
    addBubble("user", idea);
    const pending = addBubble("assistant", "…");
    pending.classList.add("pending");
    const res = await fetch(`${base}/api/chat/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idea }),
    });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    const data = await res.json();
    sessionId = data.session_id;
    pending.classList.remove("pending");
    pending.textContent = data.reply;
    renderOptions(data.options);
  } catch (e) {
    $("errorText").textContent = `요청 실패: ${e.message} (서버 주소 설정을 확인해주세요)`;
    showView("error");
  } finally {
    $("startChatBtn").disabled = false;
  }
}

async function sendChatMessage() {
  const message = $("chatInput").value.trim();
  if (!message || !sessionId) return;
  const base = getApiBase();
  $("chatInput").value = "";
  $("chatSendBtn").disabled = true;
  renderOptions([]);
  addBubble("user", message);
  const pending = addBubble("assistant", "…");
  pending.classList.add("pending");
  try {
    const res = await fetch(`${base}/api/chat/${sessionId}/reply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    const data = await res.json();
    pending.classList.remove("pending");
    pending.textContent = data.reply;
    renderOptions(data.options);
  } catch (e) {
    pending.classList.remove("pending");
    pending.textContent = `(응답 실패: ${e.message})`;
  } finally {
    $("chatSendBtn").disabled = false;
  }
}

// #stageList의 data-key가 job.stage 문자열에 포함되면 그 단계까지 진행된 것으로 본다.
const STAGE_ORDER = ["기획안", "대본", "씬 설계", "샷 분해", "영상 제작", "합본"];

function updateStageList(stageText) {
  $("stageText").textContent = stageText;
  const currentIdx = STAGE_ORDER.findIndex((key) => stageText.includes(key));
  document.querySelectorAll("#stageList li").forEach((li) => {
    const idx = STAGE_ORDER.indexOf(li.dataset.key);
    li.classList.remove("active", "done");
    if (idx < currentIdx) li.classList.add("done");
    else if (idx === currentIdx) li.classList.add("active");
  });
}

let pollTimer = null;
let currentJobMode = "video"; // "video" | "stills"
let currentJobId = null;      // 방금 완성한 영상 job — 결과 화면에서 "작품에 저장"에 사용

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function pollJob(jobId) {
  const base = getApiBase();
  try {
    const res = await fetch(`${base}/api/jobs/${jobId}`);
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    const job = await res.json();

    // 스틸 모드: 폴링마다 완성된 스틸부터 하나씩 노출(전부 끝날 때까지 안 기다림). 아직 스틸이
    // 없으면(참조 생성·상세 콘티 분할 등 파이프라인 진행 중) 현재 단계를 "생성 중…"으로 보여준다.
    if (currentJobMode === "stills") {
      showView("stills");
      const stills = job.stills || [];
      if (stills.length) {
        renderStillsList(stills, job.total || stills.length);
        if (job.status !== "done") {  // 생성 중엔 방금 나온 최신 컷을 보여준다(하나씩 노출)
          stillsPageIndex = stillsCuts.length - 1;
          renderStillsPage();
        }
      } else if (job.status !== "done") {
        renderStillsLoading(job.stage || "생성 중…");
      }
    } else if (job.status === "running") {
      updateStageList(job.stage || "진행 중");
    }

    if (job.status === "done" && currentJobMode === "stills") {
      stopPolling();
      // 스틸이 화(scene_stills)에 저장됐으니 프로젝트를 다시 받아 최종본으로 맞춘다.
      await loadStudio(studioProjectId);
      renderStills();
    } else if (job.status === "done") {
      stopPolling();
      $("resultVideo").src = `${base}${job.video_url}`;
      $("publishVideoBtn").disabled = false;
      showView("result");
    } else if (job.status === "error") {
      stopPolling();
      const rawErr = job.error || "";
      const errMsg = rawErr.includes("InputImageSensitiveContentDetected")
        ? "이미지 안전 필터에 걸렸어요. 다시 시도하면 자동으로 얼굴 가림 처리 후 재생성합니다."
        : rawErr || "알 수 없는 오류가 발생했어요.";
      $("errorText").textContent = errMsg;
      showView("error");
    }
  } catch (e) {
    stopPolling();
    $("errorText").textContent = `연결 실패: ${e.message} (서버 주소 설정을 확인해주세요)`;
    showView("error");
  }
}

// 현재 화면에 떠 있는 로그라인+인물 카드(수정 시 여기 반영, 서버에는 안 보냄 —
// 아직 이 값을 읽어가는 다음 단계가 없어서 로컬 상태로만 충분).
let currentCard = null;
let editing = false;

// 인물 이미지는 카드 텍스트와 분리해서 각자 비동기로 불러온다(화면 전환을 기다리게 안 함).
async function loadCharacterPortrait(ch, imgBoxEl) {
  try {
    const base = getApiBase();
    const res = await fetch(`${base}/api/portrait`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: ch.name || "", role: ch.role || "" }),
    });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    const { image } = await res.json();
    ch.image = image;
    imgBoxEl.outerHTML = `<img class="char-photo" src="${image}" alt="${ch.name || ""}">`;
  } catch (e) {
    imgBoxEl.textContent = "이미지 생성 실패";
  }
}

// 임팩트 장면 이미지 — 인물 초상화(characterImages)를 참조로 넘겨 같은 얼굴이 장면에 나오게 한다.
async function loadKeySceneImage(keyScene, imgBoxEl, characterImages) {
  try {
    const base = getApiBase();
    const res = await fetch(`${base}/api/scene-image`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        situation: keyScene.situation || "",
        character_images: (characterImages || []).filter(Boolean),
      }),
    });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    const { image } = await res.json();
    keyScene.image = image;
    imgBoxEl.outerHTML = `<img class="key-scene-photo" src="${image}" alt="1화 임팩트 장면">`;
  } catch (e) {
    imgBoxEl.textContent = "이미지 생성 실패";
  }
}

function renderPitchCard(card) {
  currentCard = card;
  editing = false;
  $("loglineDisplay").textContent = card.logline;
  $("loglineDisplay").classList.remove("hidden");
  $("loglineEdit").classList.add("hidden");

  const box = $("charactersBox");
  box.innerHTML = "";
  const portraitPromises = [];
  for (const ch of card.characters || []) {
    const div = document.createElement("div");
    div.className = "character-card";
    const photo = ch.image
      ? `<img class="char-photo" src="${ch.image}" alt="${ch.name || ""}">`
      : `<div class="char-photo-placeholder">이미지 생성 중…</div>`;
    div.innerHTML = `
      ${photo}
      <div class="char-name">${ch.name || ""}</div>
      <div class="char-role">${ch.role || ""}</div>
      <div class="char-line">"${ch.line || ""}"</div>
    `;
    box.appendChild(div);
    if (!ch.image) {
      portraitPromises.push(loadCharacterPortrait(ch, div.querySelector(".char-photo-placeholder")));
    }
  }

  const keyScene = card.key_scene || {};
  const imgBox = $("keySceneImageBox");
  if (keyScene.image) {
    imgBox.outerHTML = `<img id="keySceneImageBox" class="key-scene-photo" src="${keyScene.image}" alt="1화 임팩트 장면">`;
  } else {
    imgBox.textContent = "이미지 생성 중…";
    imgBox.className = "key-scene-photo-placeholder";
    // 인물 초상화가 다 만들어진 뒤에 그 얼굴을 참조로 장면을 생성한다(순서 보장).
    Promise.all(portraitPromises).then(() => {
      loadKeySceneImage(keyScene, imgBox, (card.characters || []).map((c) => c.image));
    });
  }
  $("keySceneLines").innerHTML = (keyScene.lines || [])
    .map((line) => `<div>${line}</div>`).join("");

  $("editPitchBtn").textContent = "✏️ 수정";
}

function enterEditMode() {
  editing = true;
  $("loglineDisplay").classList.add("hidden");
  $("loglineEdit").value = currentCard.logline;
  $("loglineEdit").classList.remove("hidden");

  const box = $("charactersBox");
  box.innerHTML = "";
  currentCard.characters.forEach((ch, i) => {
    const div = document.createElement("div");
    div.className = "character-card";
    div.innerHTML = `
      <input data-i="${i}" data-field="name" value="${ch.name || ""}" placeholder="이름">
      <input data-i="${i}" data-field="role" value="${ch.role || ""}" placeholder="역할/설정">
      <input data-i="${i}" data-field="line" value="${ch.line || ""}" placeholder="핵심 대사">
    `;
    box.appendChild(div);
  });
  $("editPitchBtn").textContent = "💾 저장";
}

function saveEdits() {
  currentCard.logline = $("loglineEdit").value.trim();
  document.querySelectorAll("#charactersBox input").forEach((input) => {
    currentCard.characters[Number(input.dataset.i)][input.dataset.field] = input.value.trim();
  });
  renderPitchCard(currentCard);
}

$("editPitchBtn").addEventListener("click", () => {
  if (editing) saveEdits();
  else enterEditMode();
});

async function requestPitchCard() {
  const base = getApiBase();
  const res = await fetch(`${base}/api/chat/${sessionId}/finalize`, { method: "POST" });
  if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
  return res.json();
}

async function finalizeChat() {
  // 지금은 로그라인+인물 카드까지만 만들고 멈춘다(영상까지 이어지는 전체 파이프라인은
  // 아직 안 붙임). 텍스트 카드는 빠르게 오고, 화면 전환 직후 인물 이미지는 각자 따로
  // 비동기로 채워진다(renderPitchCard 안의 loadCharacterPortrait).
  if (!sessionId) return;
  $("finalizeBtn").disabled = true;
  const original = $("finalizeBtn").textContent;
  $("finalizeBtn").textContent = "만드는 중…";
  try {
    const card = await requestPitchCard();
    renderPitchCard(card);
    showView("pitch");
  } catch (e) {
    $("errorText").textContent = `요청 실패: ${e.message} (서버 주소 설정을 확인해주세요)`;
    showView("error");
  } finally {
    $("finalizeBtn").disabled = false;
    $("finalizeBtn").textContent = original;
  }
}

async function regeneratePitch() {
  $("regenPitchBtn").disabled = true;
  const original = $("regenPitchBtn").textContent;
  $("regenPitchBtn").textContent = "재생성 중…";
  try {
    const card = await requestPitchCard();
    renderPitchCard(card);
  } catch (e) {
    $("errorText").textContent = `재생성 실패: ${e.message}`;
    showView("error");
  } finally {
    $("regenPitchBtn").disabled = false;
    $("regenPitchBtn").textContent = original;
  }
}
$("regenPitchBtn").addEventListener("click", regeneratePitch);

// 장르·기획 자체를 바꾸고 싶을 때 — 채팅 이력은 그대로 두고 화면만 되돌아간다.
$("backToChatBtn").addEventListener("click", () => showView("chat"));

// 온보딩의 마지막 지점: 확정된 임팩트 장면 컷을 "영상으로 만들 준비" 화면으로 넘김.
// 실제 영상 생성 API는 아직 연결 안 함(다음 개발 단계 — 요소 레지스트리로 얼굴 고정한
// 뒤 컷별 영상화로 이어가는 프로덕션 파이프라인은 별도로 설계됨).
$("nextStageBtn").addEventListener("click", () => {
  const image = currentCard?.key_scene?.image;
  $("videoPrepImage").src = image || "";
  $("videoPrepImage").classList.toggle("hidden", !image);
  showView("videoPrep");
});

$("videoPrepBackBtn").addEventListener("click", () => showView("pitch"));

let studioProjectId = null;
let currentStudioProject = null;

function renderStudio(project) {
  currentStudioProject = project;
  $("studioTitle").textContent =
    project.title || project.logline || project.idea || "제목 없는 작품";
  renderPublished(project.published || []);
  $("studioLogline").textContent = project.logline;
  $("studioSynopsis").textContent = project.synopsis || "";

  const roster = $("studioRoster");
  roster.innerHTML = "";
  for (const ch of project.characters || []) {
    const div = document.createElement("div");
    div.className = "roster-item";
    div.dataset.id = ch.id;
    const img = ch.image ? `<img src="${ch.image}" alt="${ch.name}">` : "";
    div.innerHTML = `${img}<div class="roster-name">${ch.name}</div>`;
    roster.appendChild(div);
  }

  const episodesBox = $("studioEpisodes");
  episodesBox.innerHTML = "";
  for (const ep of project.episodes || []) {
    const div = document.createElement("div");
    div.className = "episode-card";
    div.dataset.num = ep.num;
    const done = ep.stage === "샷분해 완료";  // 이후 단계(이미지~합본)는 아직 미연결
    div.innerHTML = `
      <div>
        <div class="episode-title">${ep.num}화</div>
        <div class="episode-stage">${ep.stage}</div>
      </div>
      <button type="button" data-num="${ep.num}" ${done ? "disabled" : ""}>${done ? "완료" : "다음 단계"}</button>
    `;
    episodesBox.appendChild(div);
  }
}

async function loadStudio(projectId) {
  const base = getApiBase();
  const res = await fetch(`${base}/api/studio/${projectId}`);
  if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
  renderStudio(await res.json());
}

function renderPublished(list) {
  const box = $("publishedList");
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = '<div class="roster-empty">아직 발행된 영상이 없어요. 영상을 만든 뒤 “작품에 저장”을 누르면 여기 모여요.</div>';
    return;
  }
  const base = getApiBase();
  for (const v of list) {
    const div = document.createElement("div");
    div.className = "published-card";
    div.innerHTML = `
      <video controls playsinline preload="metadata"
             src="${base}/api/studio/${studioProjectId}/published/${v.id}/video"></video>
      <div class="published-meta">
        <span class="published-title">${v.title || `${v.episode_num}화`}</span>
        <button type="button" class="published-del" data-id="${v.id}" title="삭제">🗑️</button>
      </div>`;
    box.appendChild(div);
  }
}

$("publishedList").addEventListener("click", async (e) => {
  const del = e.target.closest(".published-del");
  if (!del || !studioProjectId) return;
  if (!confirm("이 발행 영상을 목록에서 삭제할까요?")) return;
  const base = getApiBase();
  await fetch(`${base}/api/studio/${studioProjectId}/published/${del.dataset.id}`,
    { method: "DELETE" });
  await loadStudio(studioProjectId);
});

// 제목 편집(로그라인 위 '제목' 섹션)
$("editTitleBtn").addEventListener("click", () => {
  $("studioTitleInput").value =
    (currentStudioProject && currentStudioProject.title) || "";
  $("studioTitle").classList.add("hidden");
  $("editTitleBtn").classList.add("hidden");
  $("titleEditRow").classList.remove("hidden");
});

function exitTitleEdit() {
  $("titleEditRow").classList.add("hidden");
  $("studioTitle").classList.remove("hidden");
  $("editTitleBtn").classList.remove("hidden");
}

$("cancelTitleBtn").addEventListener("click", exitTitleEdit);

$("saveTitleBtn").addEventListener("click", async () => {
  if (!studioProjectId) return;
  const btn = $("saveTitleBtn");
  btn.disabled = true;
  try {
    const base = getApiBase();
    const res = await fetch(`${base}/api/studio/${studioProjectId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: $("studioTitleInput").value.trim() }),
    });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    renderStudio(await res.json());
    exitTitleEdit();
  } catch (e) {
    alert(`제목 저장 실패: ${e.message}`);
  } finally {
    btn.disabled = false;
  }
});

$("editSynopsisBtn").addEventListener("click", () => {
  $("studioSynopsisEdit").value = (currentStudioProject && currentStudioProject.synopsis) || "";
  $("studioSynopsis").classList.add("hidden");
  $("editSynopsisBtn").classList.add("hidden");
  $("studioSynopsisEdit").classList.remove("hidden");
  $("synopsisEditActions").classList.remove("hidden");
});

function exitSynopsisEdit() {
  $("studioSynopsisEdit").classList.add("hidden");
  $("synopsisEditActions").classList.add("hidden");
  $("studioSynopsis").classList.remove("hidden");
  $("editSynopsisBtn").classList.remove("hidden");
}

$("cancelSynopsisBtn").addEventListener("click", exitSynopsisEdit);

$("saveSynopsisBtn").addEventListener("click", async () => {
  if (!studioProjectId) return;
  const btn = $("saveSynopsisBtn");
  btn.disabled = true;
  try {
    const base = getApiBase();
    const res = await fetch(`${base}/api/studio/${studioProjectId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ synopsis: $("studioSynopsisEdit").value.trim() }),
    });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    renderStudio(await res.json());
    exitSynopsisEdit();
  } catch (e) {
    alert(`줄거리 저장 실패: ${e.message}`);
  } finally {
    btn.disabled = false;
  }
});

$("genSynopsisBtn").addEventListener("click", async () => {
  if (!studioProjectId) return;
  if (!revealNoteThenProceed("synopsisNoteInput")) return;
  const btn = $("genSynopsisBtn");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "생성 중…";
  try {
    const base = getApiBase();
    const res = await fetch(`${base}/api/studio/${studioProjectId}/generate-synopsis`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: $("synopsisNoteInput").value.trim() }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
    }
    renderStudio(await res.json());
    hideNote("synopsisNoteInput");
  } catch (e) {
    alert(`전체 줄거리 AI 생성 실패: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

$("studioEpisodes").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-num]");
  if (!btn || !studioProjectId) return;
  const num = btn.dataset.num;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "진행 중…";
  try {
    const base = getApiBase();
    const res = await fetch(`${base}/api/studio/${studioProjectId}/episodes/${num}/advance`, {
      method: "POST",
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
    }
    await loadStudio(studioProjectId);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = original;
    alert(`다음 단계 진행 실패: ${err.message}`);
  }
});

$("studioEpisodes").addEventListener("click", (e) => {
  if (e.target.closest("button[data-num]")) return;
  const card = e.target.closest(".episode-card[data-num]");
  if (card) openEpisodeDetail(Number(card.dataset.num));
});

let currentEpisodeNum = null;

function currentEpisode() {
  return (currentStudioProject.episodes || []).find((e) => e.num === currentEpisodeNum);
}

function renderEpisodeDetail() {
  const ep = currentEpisode();
  if (!ep) return;
  $("episodeDetailTitle").textContent =
    ep.subtitle ? `${ep.num}화 — ${ep.subtitle}` : `${ep.num}화`;

  const ids = new Set(ep.character_ids || []);
  const epChars = (currentStudioProject.characters || []).filter((c) => ids.has(c.id));
  const roster = $("episodeDetailRoster");
  roster.innerHTML = "";
  if (!epChars.length) {
    roster.innerHTML = `<div class="roster-empty">아직 없음 — [+ 추가]로 등장인물을 넣으세요.</div>`;
  }
  for (const ch of epChars) {
    const div = document.createElement("div");
    div.className = "roster-item";
    const img = ch.image ? `<img src="${ch.image}" alt="${ch.name}">` : "";
    div.innerHTML = `${img}<div class="roster-name">${ch.name}</div>`;
    roster.appendChild(div);
  }

  $("episodeDetailSummary").textContent = ep.summary || "(아직 없음)";
  $("episodeDetailScript").innerHTML = renderScriptMarkdown(ep.script);
}

function openEpisodeDetail(num) {
  currentEpisodeNum = num;
  stillsPageIndex = 0; // 다른 화 열 때 스틸 페이지 처음으로
  saveLastOpen(studioProjectId, num);
  // 편집 모드 초기화(다른 화 열 때 이전 편집 상태가 남지 않게)
  exitSubtitleEdit();
  exitSummaryEdit();
  exitScriptEdit();
  renderEpisodeDetail();
  showView("episodeDetail");
}

$("closeEpisodeDetailBtn").addEventListener("click", () => {
  saveLastOpen(studioProjectId, null);
  showView("studio");
});

function startJob(endpoint, mode, prepMsg, query) {
  const base = getApiBase();
  const qs = query ? `?${new URLSearchParams(query)}` : "";
  return fetch(`${base}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}/${endpoint}${qs}`,
    { method: "POST" })
    .then(async (res) => {
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
      }
      const { job_id } = await res.json();
      currentJobId = job_id;
      currentJobMode = mode;
      // ★2026-07-22: 스틸 모드는 별도 진행 화면 없이 곧바로 스틸 뷰(생성 중…)로 — 백엔드
      // 파이프라인(참조 생성·붙이기·상세 콘티 분할)이 도는 동안 폴링이 완성된 컷부터 노출한다.
      if (mode === "stills") {
        stillsPageIndex = 0;
        showView("stills");
        renderStillsLoading(prepMsg);
      } else {
        showView("progress");
        updateStageList(prepMsg);
      }
      stopPolling();
      pollTimer = setInterval(() => pollJob(job_id), 3000);
      pollJob(job_id);
    });
}

// "드라마 만들기"는 곧바로 영상이 아니라 먼저 씬별 스틸컷 미리보기를 만든다(영상 만들기 전 확인).
// ★2026-07-21(사용자 지시): 전체 씬을 한 번에 만들지 않고 1씬만 먼저 만든다 — 이후 화면에서
// "+ 다음 씬 만들기" 버튼으로 사용자가 원할 때마다 다음 씬을 하나씩 추가한다.
// v3.1 엔진(scene→clip→block) 미리보기/제작 중인지. true면 "다음 씬"·"영상 만들기"가 v3
// 엔드포인트(v3/preview-scene, v3/produce)로 라우팅되고 스틸뷰에서 v3 제작 버튼을 보여준다.
// ★2026-07-22: v3.1 엔진만 남겨 항상 true(구 샷 경로 UI 제거). 구 makeDramaBtn 핸들러는
// 숨겨진 채 남아있고 트리거되지 않으므로 v3Mode를 false로 되돌리는 경로가 없다.
let v3Mode = true;

function _setV3Buttons(on) {
  v3Mode = on;
  const mk = $("makeVideoFromStillsBtn");
  const v3 = $("v3ProduceBtn");
  if (mk) mk.classList.toggle("hidden", on);       // v3 모드에선 구 produce 숨김
  if (v3) v3.classList.toggle("hidden", !on);
}

$("makeDramaBtn").addEventListener("click", async () => {
  const ep = currentEpisode();
  if (!ep) return;
  if (!ep.script) {
    alert("대본이 먼저 있어야 해요. 대본을 AI 생성하거나 작성해주세요.");
    return;
  }
  _setV3Buttons(false);
  try {
    await startJob("preview-stills", "stills", "장면 미리보기 준비 중", { scene_num: 1 });
  } catch (e) {
    alert(`장면 미리보기 실패: ${e.message}`);
  }
});

$("v3PreviewBtn").addEventListener("click", async () => {
  const ep = currentEpisode();
  if (!ep) return;
  if (!ep.script) {
    alert("대본이 먼저 있어야 해요. 대본을 AI 생성하거나 작성해주세요.");
    return;
  }
  _setV3Buttons(true);
  // ★2026-07-22: 이미 만들어둔 스틸이 있으면 재생성하지 말고 그대로 띄운다(중복 생성·과부하 방지).
  // 다시 만들려면 스틸 뷰에서 컷을 지우고 "씬1부터 만들기"를 쓰면 된다.
  if ((ep.scene_stills || []).some((s) => s.image)) {
    showView("stills");
    renderStills();
    return;
  }
  try {
    await startJob("v3/preview-scene", "stills", "v3.1 미리보기 준비 중", { scene_num: 1 });
  } catch (e) {
    alert(`v3.1 미리보기 실패: ${e.message}`);
  }
});

$("v3ProduceBtn").addEventListener("click", async () => {
  if (!confirm("v3.1 엔진으로 이 화 전체를 씬 순서대로 자동 제작·합본할까요? 몇 분 걸려요.")) return;
  try {
    await startJob("v3/produce", "video", "v3.1 영상 제작 준비 중");
  } catch (e) {
    alert(`v3.1 영상 제작 실패: ${e.message}`);
  }
});

$("nextSceneBtn").addEventListener("click", async () => {
  const ep = currentEpisode();
  const next = nextUnmadeSceneNum(ep);
  if (!next) return;
  const endpoint = v3Mode ? "v3/preview-scene" : "preview-stills";
  try {
    await startJob(endpoint, "stills", `씬${next} 준비 중`, { scene_num: next });
  } catch (e) {
    alert(`씬 생성 실패: ${e.message}`);
  }
});

// scene_lines(전체 씬 목록)와 scene_stills(이미 만들어진 씬들)를 비교해 아직 안 만든 다음 씬
// 번호를 찾는다. scene_lines를 아직 모르면(첫 미리보기 전) null.
function nextUnmadeSceneNum(ep) {
  if (!ep || !ep.scene_lines || !ep.scene_lines.length) return null;
  const done = new Set((ep.scene_stills || []).map((s) => s.scene_num));
  for (const [n] of ep.scene_lines) {
    if (!done.has(n)) return n;
  }
  return null;
}

// 기존 프로젝트에는 예전 방식으로 만든 여러 컷이 남아 있을 수 있다. 데이터는 삭제하지 않고
// shots_by_scene의 대표 샷 선택 기준(등장인물 수가 가장 많고, 동률이면 앞 컷)에 맞는 한 장만
// 골라 보여준다. 새 데이터는 서버가 애초에 대표 컷 한 장만 내려준다.
function representativePreviewItems(items) {
  const ep = currentEpisode();
  const shotsByScene = (ep && ep.shots_by_scene) || {};
  const groups = new Map();
  for (const item of items || []) {
    const group = groups.get(item.scene_num) || [];
    group.push(item);
    groups.set(item.scene_num, group);
  }

  const selected = [];
  for (const [sceneNum, group] of groups) {
    const marked = group.find((item) => item.representative);
    if (marked) {
      selected.push(marked);
      continue;
    }
    const shots = shotsByScene[sceneNum] || shotsByScene[String(sceneNum)] || [];
    const representative = [...shots].sort((a, b) =>
      ((b.characters || []).length - (a.characters || []).length)
      || ((a.n || 0) - (b.n || 0)))[0];
    selected.push(group.find((item) => item.cut_num === representative?.n) || group[0]);
  }
  return selected;
}

// 스틸 페이지네이션 상태 — 한 번에 한 장씩 보여주고 ◀ 이전 / 다음 ▶ 로 넘긴다(가로 스크롤 대신).
let stillsCuts = [];
let stillsPageIndex = 0;
// 영상화 진행 중인 컷들("scene-cut") — 페이지를 넘겨 카드가 다시 그려져도 "영상화 중" 상태를 유지.
const videoizingCuts = new Set();
const cutKey = (scene, cut) => `${scene}-${cut}`;

// 그 컷의 저장된 영상 URL(scene_stills[].video_path가 있으면 안정 엔드포인트로 서빙). 캐시 무력화용 t.
function cutVideoUrl(scene, cut) {
  return `${getApiBase()}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}/cuts/${scene}/${cut}/video?t=${Date.now()}`;
}

function _stillCardEl(c) {
  const div = document.createElement("div");
  div.className = "still-card";
  div.dataset.scene = c.scene_num;
  div.dataset.cut = c.cut_num;
  const busy = videoizingCuts.has(cutKey(c.scene_num, c.cut_num));
  const hasVideo = !!c.video_path;
  const media = hasVideo
    ? `<video controls playsinline src="${cutVideoUrl(c.scene_num, c.cut_num)}"></video>`
    : (c.image ? `<img src="${c.image}" alt="씬${c.scene_num} 컷${c.cut_num}">` : "");
  const vLabel = busy ? "영상화 중…" : (hasVideo ? "🎬 다시 영상화" : "🎬 영상화");
  div.innerHTML = `
    <div class="still-media">${media}</div>
    <div class="still-title">씬${c.scene_num} · 컷${c.cut_num}</div>
    <div class="still-caption">${c.caption || ""}</div>
    <div class="still-cut-actions">
      <button type="button" class="text-btn cut-regen-btn">🔁 재생성</button>
      <button type="button" class="text-btn cut-videoize-btn"${busy ? " disabled" : ""}>${vLabel}</button>
      <button type="button" class="text-btn cut-delete-btn">🗑️ 삭제</button>
    </div>
    <div class="cut-note hidden">
      <textarea class="cut-note-textarea gen-note-textarea" rows="2" placeholder="영상에 반영할 의견(선택) — 예: 카메라 더 천천히, 표정 강조"></textarea>
      <div class="gen-note-actions">
        <button type="button" class="text-btn cut-note-cancel-btn">취소</button>
        <button type="button" class="text-btn cut-note-submit-btn">🎬 영상 생성</button>
      </div>
    </div>`;
  return div;
}

// 스틸이 아직 하나도 안 나온 로딩 상태 — 백엔드 파이프라인 현재 단계(stage)를 "생성 중…"으로.
function renderStillsLoading(stage) {
  $("stillsList").innerHTML = `<div class="roster-empty">🎬 ${escapeHtml(stage || "생성 중…")}</div>`;
}

// stillsCuts[stillsPageIndex] 한 장 + 페이지네이션 바를 그린다. 인덱스는 범위를 벗어나면 보정.
function renderStillsPage() {
  const list = $("stillsList");
  list.innerHTML = "";
  if (!stillsCuts.length) return;
  stillsPageIndex = Math.max(0, Math.min(stillsPageIndex, stillsCuts.length - 1));
  list.appendChild(_stillCardEl(stillsCuts[stillsPageIndex]));
  const pager = document.createElement("div");
  pager.className = "stills-pager";
  pager.innerHTML = `
    <button type="button" class="text-btn stills-prev-btn"${stillsPageIndex === 0 ? " disabled" : ""}>◀ 이전</button>
    <span class="stills-counter">${stillsPageIndex + 1} / ${stillsCuts.length}</span>
    <button type="button" class="text-btn stills-next-btn"${stillsPageIndex === stillsCuts.length - 1 ? " disabled" : ""}>다음 ▶</button>`;
  list.appendChild(pager);
}

function renderStillsList(items, total) {
  const list = $("stillsList");
  list.innerHTML = "";
  // v3.1 스틸은 클립마다 한 장씩(각자 clip_id 보유) — 전부 보여준다. 구 파이프라인만 씬당 대표 1장.
  const isV3 = (items || []).some((it) => it.clip_id != null);
  stillsCuts = (isV3 ? [...(items || [])] : representativePreviewItems(items)).sort((a, b) =>
    (a.scene_num - b.scene_num) || ((a.cut_num || 0) - (b.cut_num || 0)));
  if (!stillsCuts.length) {
    if (total) {
      list.innerHTML = `<div class="roster-empty">생성 중...</div>`;
    } else {
      // 스틸컷을 전부 삭제했거나 아직 아무것도 안 만든 상태 — 씬1부터 다시 만드는 카드형 버튼
      // (스틸컷 이미지와 같은 크기). 삭제로 진입점이 사라지는 문제를 여기서 되살린다.
      const mk = document.createElement("button");
      mk.type = "button";
      mk.className = "still-card make-scene-card";
      mk.innerHTML = `<div class="still-media make-scene-plus">＋</div>
        <div class="still-title">씬1부터 스틸컷 만들기</div>`;
      list.appendChild(mk);
    }
    return;
  }
  renderStillsPage();
}

function renderStills() {
  const ep = currentEpisode();
  const stills = (ep && ep.scene_stills) || [];
  renderStillsList(stills, stills.length);
  const next = nextUnmadeSceneNum(ep);
  const btn = $("nextSceneBtn");
  if (next) {
    btn.textContent = `+ 다음 씬 만들기 (씬${next})`;
    btn.classList.remove("hidden");
  } else {
    btn.classList.add("hidden");
  }
}

$("stillsBackBtn").addEventListener("click", () => showView("episodeDetail"));

// 컷 영상화 job 폴링 — 카드 참조 대신 (scene,cut)로 추적한다. 완료/실패 시 videoizingCuts에서
// 빼고 화 데이터를 다시 불러 페이지를 다시 그린다 → 영상이 그 컷에 저장돼 페이지를 넘겨도 유지된다.
function pollCutVideoJob(jobId, sceneNum, cutNum) {
  const base = getApiBase();
  const finish = async (msg) => {
    videoizingCuts.delete(cutKey(sceneNum, cutNum));
    try { await loadStudio(studioProjectId); } catch (e) { /* 무시 */ }
    renderStills(); // ★낡은 stillsCuts가 아니라 새로 불러온 화 데이터로 다시 만든다(영상 반영)
    if (msg) alert(msg);
  };
  const check = async () => {
    try {
      const res = await fetch(`${base}/api/jobs/${jobId}`);
      if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
      const job = await res.json();
      if (job.status === "done") { await finish(null); return; }
      if (job.status === "error") {
        const rawErr = job.error || "";
        await finish("영상화 실패: " + (rawErr.includes("InputImageSensitiveContentDetected")
          ? "안전 필터에 걸렸어요. 다시 시도해보세요." : (rawErr || "영상화 실패")));
        return;
      }
      setTimeout(check, 3000);
    } catch (e) {
      await finish(`연결 실패: ${e.message}`);
    }
  };
  setTimeout(check, 3000);
}

// 컷 카드의 재생성/영상화 버튼 — 이벤트 위임(카드는 매번 다시 그려지므로).
$("stillsList").addEventListener("click", async (e) => {
  // 페이지네이션(◀ 이전 / 다음 ▶) — 스틸 카드보다 먼저 처리(이 버튼들은 .still-card 밖에 있음).
  if (e.target.closest(".stills-prev-btn")) {
    if (stillsPageIndex > 0) { stillsPageIndex--; renderStillsPage(); }
    return;
  }
  if (e.target.closest(".stills-next-btn")) {
    if (stillsPageIndex < stillsCuts.length - 1) { stillsPageIndex++; renderStillsPage(); }
    return;
  }

  const card = e.target.closest(".still-card");
  if (!card || !studioProjectId || !currentEpisodeNum) return;
  const sceneNum = card.dataset.scene;
  const cutNum = card.dataset.cut;
  const base = getApiBase();

  // 빈 상태의 "씬1부터 스틸컷 만들기" 카드 — scene_stills가 비어 있으므로 씬1부터 새로 만든다.
  // v3.1 미리보기 중이면(v3Mode) v3 엔드포인트로, 그 상태가 새로고침 등으로 리셋됐어도 이 화가
  // v3로 만들어졌으면(v3_scenes 존재) v3 경로로 라우팅한다.
  if (card.classList.contains("make-scene-card")) {
    const ep = currentEpisode();
    const isV3 = v3Mode || !!(ep && ep.v3_scenes && ep.v3_scenes.length);
    const endpoint = isV3 ? "v3/preview-scene" : "preview-stills";
    _setV3Buttons(isV3);
    try {
      await startJob(endpoint, "stills", "씬1 준비 중", { scene_num: 1 });
    } catch (err) {
      alert(`씬 생성 실패: ${err.message}`);
    }
    return;
  }

  if (e.target.closest(".cut-regen-btn")) {
    const btn = e.target.closest(".cut-regen-btn");
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "재생성 중…";
    try {
      const res = await fetch(
        `${base}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}/cuts/${sceneNum}/${cutNum}/regenerate`,
        { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
      }
      const newStill = await res.json();
      card.querySelector(".still-media").innerHTML =
        `<img src="${newStill.image}" alt="씬${sceneNum} 컷${cutNum}">`;
      await loadStudio(studioProjectId); // 화 데이터도 갱신해둬(다음 재생성/영상화가 최신 스틸을 보게)
    } catch (err) {
      alert(`이미지 재생성 실패: ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
    return;
  }

  // 🎬 영상화(또는 다시 영상화) — 바로 만들지 않고 AI 생성과 같은 툴팁으로 의견을 먼저 묻는다.
  if (e.target.closest(".cut-videoize-btn")) {
    const box = card.querySelector(".cut-note");
    if (box) {
      box.classList.remove("hidden");
      const ta = box.querySelector(".cut-note-textarea");
      if (ta) ta.focus();
    }
    return;
  }
  if (e.target.closest(".cut-note-cancel-btn")) {
    const box = card.querySelector(".cut-note");
    if (box) { box.querySelector(".cut-note-textarea").value = ""; box.classList.add("hidden"); }
    return;
  }
  if (e.target.closest(".cut-note-submit-btn")) {
    const box = card.querySelector(".cut-note");
    const note = box ? box.querySelector(".cut-note-textarea").value.trim() : "";
    videoizingCuts.add(cutKey(sceneNum, cutNum));
    renderStillsPage(); // "영상화 중…" 상태로 갱신(페이지를 넘겨도 유지)
    try {
      const q = note ? `?note=${encodeURIComponent(note)}` : "";
      const res = await fetch(
        `${base}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}/cuts/${sceneNum}/${cutNum}/videoize${q}`,
        { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
      }
      const { job_id } = await res.json();
      pollCutVideoJob(job_id, sceneNum, cutNum);
    } catch (err) {
      videoizingCuts.delete(cutKey(sceneNum, cutNum));
      renderStillsPage();
      alert(`영상화 실패: ${err.message}`);
    }
    return;
  }

  if (e.target.closest(".cut-delete-btn")) {
    if (!confirm(`씬${sceneNum} · 컷${cutNum}을 삭제할까요? 다시 만들려면 "씬 만들기"를 눌러야 해요.`)) return;
    const btn = e.target.closest(".cut-delete-btn");
    btn.disabled = true;
    try {
      const res = await fetch(
        `${base}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}/cuts/${sceneNum}/${cutNum}`,
        { method: "DELETE" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
      }
      card.remove();
      await loadStudio(studioProjectId); // scene_stills/v3_scenes 최신 상태로 갱신(다음 씬 판단용)
      renderStills(); // 전부 지웠으면 "씬1부터 만들기" 카드 노출 + 다음 씬 버튼 갱신
    } catch (err) {
      alert(`삭제 실패: ${err.message}`);
      btn.disabled = false;
    }
    return;
  }
});

// 미리보기에서 만든 씬·샷을 그대로 재사용해 이미지→영상→합본까지 제작한다(개별 컷을 다
// 검토·영상화한 뒤 한 번에 합치고 싶을 때 쓰는 전체 자동 경로 — 기본 흐름은 위 컷별 버튼).
$("makeVideoFromStillsBtn").addEventListener("click", async () => {
  if (!confirm("모든 씬의 모든 컷을 자동으로 이미지→영상화하고 합본까지 만들어요. 시간이 꽤 걸리고, 개별로 검토·재생성한 컷도 다시 만들어질 수 있어요. 계속할까요?")) return;
  try {
    await startJob("produce", "video", "영상 제작 준비 중");
  } catch (e) {
    alert(`드라마 만들기 실패: ${e.message}`);
  }
});

// 편집 후 서버 반영 → currentStudioProject 갱신 → 상세 재렌더의 공통 처리
async function patchEpisode(body) {
  const base = getApiBase();
  const res = await fetch(
    `${base}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}`,
    { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
  );
  if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
  await loadStudio(studioProjectId);
  renderEpisodeDetail();
}

// ── 부제목 ──
function exitSubtitleEdit() { $("subtitleEditRow").classList.add("hidden"); }
$("editSubtitleBtn").addEventListener("click", () => {
  $("episodeSubtitleInput").value = currentEpisode().subtitle || "";
  $("subtitleEditRow").classList.remove("hidden");
});
$("cancelSubtitleBtn").addEventListener("click", exitSubtitleEdit);
$("saveSubtitleBtn").addEventListener("click", async () => {
  try {
    await patchEpisode({ subtitle: $("episodeSubtitleInput").value.trim() });
    exitSubtitleEdit();
  } catch (e) { alert(`부제목 저장 실패: ${e.message}`); }
});

// ── 요약 (수정 / AI 생성) ──
function exitSummaryEdit() {
  $("episodeSummaryEdit").classList.add("hidden");
  $("summaryEditActions").classList.add("hidden");
  $("episodeDetailSummary").classList.remove("hidden");
}
$("editSummaryBtn").addEventListener("click", () => {
  $("episodeSummaryEdit").value = currentEpisode().summary || "";
  $("episodeDetailSummary").classList.add("hidden");
  $("episodeSummaryEdit").classList.remove("hidden");
  $("summaryEditActions").classList.remove("hidden");
});
$("cancelSummaryBtn").addEventListener("click", exitSummaryEdit);
$("saveSummaryBtn").addEventListener("click", async () => {
  try {
    await patchEpisode({ summary: $("episodeSummaryEdit").value.trim() });
    exitSummaryEdit();
  } catch (e) { alert(`요약 저장 실패: ${e.message}`); }
});
$("genSummaryBtn").addEventListener("click", async () => {
  if (!revealNoteThenProceed("summaryNoteInput")) return;
  const btn = $("genSummaryBtn");
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "생성 중…";
  try {
    const base = getApiBase();
    const res = await fetch(
      `${base}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}/generate-summary`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: $("summaryNoteInput").value.trim() }),
      }
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
    }
    await loadStudio(studioProjectId);
    renderEpisodeDetail();
    hideNote("summaryNoteInput");
  } catch (e) { alert(`요약 AI 생성 실패: ${e.message}`); }
  finally { btn.disabled = false; btn.textContent = original; }
});

// ── 대본 (수정 / AI 생성) ──
function exitScriptEdit() {
  $("episodeScriptEdit").classList.add("hidden");
  $("scriptEditActions").classList.add("hidden");
  $("episodeDetailScript").classList.remove("hidden");
}
$("editScriptBtn").addEventListener("click", () => {
  $("episodeScriptEdit").value = currentEpisode().script || "";
  $("episodeDetailScript").classList.add("hidden");
  $("episodeScriptEdit").classList.remove("hidden");
  $("scriptEditActions").classList.remove("hidden");
});
$("cancelScriptBtn").addEventListener("click", exitScriptEdit);
$("saveScriptBtn").addEventListener("click", async () => {
  try {
    await patchEpisode({ script: $("episodeScriptEdit").value.trim() });
    exitScriptEdit();
  } catch (e) { alert(`대본 저장 실패: ${e.message}`); }
});
$("genScriptBtn").addEventListener("click", async () => {
  if (!revealNoteThenProceed("scriptNoteInput")) return;
  const btn = $("genScriptBtn");
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "생성 중…";
  try {
    const base = getApiBase();
    const res = await fetch(
      `${base}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}/generate-script`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: $("scriptNoteInput").value.trim() }),
      }
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
    }
    await loadStudio(studioProjectId);
    renderEpisodeDetail();
    hideNote("scriptNoteInput");
  } catch (e) { alert(`대본 AI 생성 실패: ${e.message}`); }
  finally { btn.disabled = false; btn.textContent = original; }
});

// ── 등장인물 추가/삭제 팝업 ──
function renderCharPicker() {
  const ep = currentEpisode();
  const ids = new Set(ep.character_ids || []);
  const list = $("charPickerList");
  list.innerHTML = "";
  const chars = currentStudioProject.characters || [];
  if (!chars.length) {
    list.innerHTML = `<div class="roster-empty">등록된 캐릭터가 없어요. 스튜디오에서 먼저 캐릭터를 추가하세요.</div>`;
    return;
  }
  for (const ch of chars) {
    const inEp = ids.has(ch.id);
    const row = document.createElement("div");
    row.className = "char-picker-row";
    row.innerHTML = `
      <span>${ch.name}${ch.role ? ` <span class="muted">(${ch.role})</span>` : ""}</span>
      <button type="button" data-id="${ch.id}" class="${inEp ? "in" : ""}">${inEp ? "삭제" : "추가"}</button>
    `;
    list.appendChild(row);
  }
}
$("episodeAddCharBtn").addEventListener("click", () => {
  renderCharPicker();
  $("charPickerModal").classList.remove("hidden");
});
$("closeCharPickerBtn").addEventListener("click", () => $("charPickerModal").classList.add("hidden"));
$("charPickerList").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-id]");
  if (!btn) return;
  const ep = currentEpisode();
  const ids = new Set(ep.character_ids || []);
  const id = btn.dataset.id;
  if (ids.has(id)) ids.delete(id); else ids.add(id);
  try {
    await patchEpisode({ character_ids: [...ids] });
    renderCharPicker();
  } catch (err) { alert(`등장인물 변경 실패: ${err.message}`); }
});

let currentCharacterId = null;

function openCharacterDetail(charId) {
  const ch = (currentStudioProject.characters || []).find((c) => c.id === charId);
  if (!ch) return;
  currentCharacterId = charId;
  $("charNameInput").value = ch.name || "";
  $("charGenderInput").value = ch.gender || "";
  $("charAgeInput").value = ch.age || "";
  $("charRoleInput").value = ch.role || "";
  $("charLineInput").value = ch.line || "";
  $("charAppearanceInput").value = ch.appearance || "";
  $("charDescriptionInput").value = ch.description || "";
  $("characterDetailImageBox").innerHTML = ch.image
    ? `<img src="${ch.image}" alt="${ch.name}">`
    : "이미지 없음";
  showView("characterDetail");
}

$("studioRoster").addEventListener("click", (e) => {
  const item = e.target.closest(".roster-item[data-id]");
  if (item) openCharacterDetail(item.dataset.id);
});

$("addCharacterBtn").addEventListener("click", async () => {
  if (!studioProjectId) return;
  const base = getApiBase();
  const res = await fetch(`${base}/api/studio/${studioProjectId}/characters`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "새 캐릭터", role: "", line: "" }),
  });
  if (!res.ok) {
    alert("캐릭터 추가 실패");
    return;
  }
  const ch = await res.json();
  await loadStudio(studioProjectId);
  openCharacterDetail(ch.id);
});

$("saveCharacterBtn").addEventListener("click", async () => {
  if (!studioProjectId || !currentCharacterId) return;
  const base = getApiBase();
  const res = await fetch(
    `${base}/api/studio/${studioProjectId}/characters/${currentCharacterId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("charNameInput").value.trim(),
        gender: $("charGenderInput").value,
        age: $("charAgeInput").value.trim(),
        role: $("charRoleInput").value.trim(),
        line: $("charLineInput").value.trim(),
        appearance: $("charAppearanceInput").value.trim(),
        description: $("charDescriptionInput").value.trim(),
      }),
    }
  );
  if (!res.ok) {
    alert("저장 실패");
    return;
  }
  await loadStudio(studioProjectId);
  showView("studio");
});

$("deleteCharacterBtn").addEventListener("click", async () => {
  if (!studioProjectId || !currentCharacterId) return;
  if (!confirm("이 캐릭터를 삭제할까요?")) return;
  const base = getApiBase();
  const res = await fetch(
    `${base}/api/studio/${studioProjectId}/characters/${currentCharacterId}`,
    { method: "DELETE" }
  );
  if (!res.ok) {
    alert("삭제 실패");
    return;
  }
  await loadStudio(studioProjectId);
  showView("studio");
});

$("regenPortraitBtn").addEventListener("click", async () => {
  if (!studioProjectId || !currentCharacterId) return;
  const btn = $("regenPortraitBtn");
  btn.disabled = true;
  btn.textContent = "생성 중…";
  try {
    const base = getApiBase();
    const name = $("charNameInput").value.trim();
    const role = $("charRoleInput").value.trim();
    const gender = $("charGenderInput").value;
    const age = $("charAgeInput").value.trim();
    const appearance = $("charAppearanceInput").value.trim();
    const portraitRes = await fetch(`${base}/api/portrait`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, role, gender, age, appearance }),
    });
    if (!portraitRes.ok) throw new Error("이미지 생성 실패");
    const { image } = await portraitRes.json();
    const updateRes = await fetch(
      `${base}/api/studio/${studioProjectId}/characters/${currentCharacterId}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image }),
      }
    );
    if (!updateRes.ok) throw new Error("이미지 저장 실패");
    $("characterDetailImageBox").innerHTML = `<img src="${image}" alt="${name}">`;
    await loadStudio(studioProjectId);
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "🔁 사진 다시 생성";
  }
});

$("genCharacterBtn").addEventListener("click", async () => {
  const name = $("charNameInput").value.trim();
  if (!name) {
    alert("이름을 먼저 입력해주세요.");
    return;
  }
  if (!revealNoteThenProceed("charHintInput")) return;
  const btn = $("genCharacterBtn");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "생성 중…";
  try {
    const base = getApiBase();
    // 이미 채운 칸은 그대로 유지되고 빈 칸만 AI가 채운다 — 현재 입력값을 전부 함께 보낸다.
    const res = await fetch(`${base}/api/studio/${studioProjectId}/characters/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        hint: $("charHintInput").value.trim(),
        gender: $("charGenderInput").value,
        age: $("charAgeInput").value.trim(),
        role: $("charRoleInput").value.trim(),
        line: $("charLineInput").value.trim(),
        appearance: $("charAppearanceInput").value.trim(),
        description: $("charDescriptionInput").value.trim(),
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
    }
    const f = await res.json();
    // 생성 결과로 입력칸을 채운다(채워둔 칸은 서버가 그대로 돌려줌). 자동 저장 X — 검토 후 "저장"
    $("charGenderInput").value = f.gender || "";
    $("charAgeInput").value = f.age || "";
    $("charRoleInput").value = f.role || "";
    $("charLineInput").value = f.line || "";
    $("charAppearanceInput").value = f.appearance || "";
    $("charDescriptionInput").value = f.description || "";
    hideNote("charHintInput");
  } catch (e) {
    alert(`캐릭터 AI 생성 실패: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

$("closeCharacterDetailBtn").addEventListener("click", () => showView("studio"));

$("goToStudioBtn").addEventListener("click", async () => {
  if (!currentCard) return;
  $("goToStudioBtn").disabled = true;
  try {
    const base = getApiBase();
    const res = await fetch(`${base}/api/studio/create`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        idea: currentCard.idea || currentCard.logline,
        logline: currentCard.logline,
        characters: currentCard.characters || [],
        key_scene: currentCard.key_scene || null,
      }),
    });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    const { project_id } = await res.json();
    studioProjectId = project_id;
    saveLastOpen(studioProjectId, null);
    await loadStudio(project_id);
    showView("studio");
  } catch (e) {
    $("errorText").textContent = `요청 실패: ${e.message} (서버 주소 설정을 확인해주세요)`;
    showView("error");
  } finally {
    $("goToStudioBtn").disabled = false;
  }
});

$("seedDemoBtn").addEventListener("click", async () => {
  // (개발용) 로그라인·줄거리·캐릭터 2명·1화(대본)까지 채워진 더미 작품을 만든 뒤
  // 내 작품 목록으로 돌아가 카드로 보여준다(스튜디오 → 내 작품 → 상세 흐름 유지).
  const btn = $("seedDemoBtn");
  btn.disabled = true;
  try {
    const base = getApiBase();
    const res = await fetch(`${base}/api/studio/seed`, { method: "POST" });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    await openWorks();
  } catch (e) {
    $("errorText").textContent = `요청 실패: ${e.message} (서버 주소 설정을 확인해주세요)`;
    showView("error");
  } finally {
    btn.disabled = false;
  }
});

$("addEpisodeBtn").addEventListener("click", async () => {
  if (!studioProjectId) return;
  const base = getApiBase();
  await fetch(`${base}/api/studio/${studioProjectId}/episodes`, { method: "POST" });
  await loadStudio(studioProjectId);
});

// ── 작품 관리 페이지(카드 목록) ──
function renderWorks(projects) {
  const box = $("worksList");
  box.innerHTML = "";
  if (!projects || !projects.length) {
    box.innerHTML = '<p class="modal-hint">아직 만든 작품이 없어요. “+ 새 작품”으로 시작해보세요.</p>';
    return;
  }
  for (const p of projects) {
    const card = document.createElement("div");
    card.className = "work-card";
    card.dataset.id = p.id;
    const title = document.createElement("div");
    title.className = "work-card-title";
    title.textContent = p.title || "제목 없는 작품";
    const meta = document.createElement("div");
    meta.className = "work-card-meta";
    meta.innerHTML =
      `<span class="work-stage">${p.stage}</span>` +
      `<span class="work-eps">${p.episode_count}화</span>`;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "work-card-del";
    del.dataset.id = p.id;
    del.title = "삭제";
    del.textContent = "🗑️";
    card.append(title, meta, del);
    box.appendChild(card);
  }
}

async function openWorks() {
  const base = getApiBase();
  showView("works");
  $("worksList").innerHTML = '<p class="modal-hint">불러오는 중…</p>';
  try {
    const res = await fetch(`${base}/api/studio`);
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    const { projects } = await res.json();
    renderWorks(projects);
  } catch (e) {
    $("worksList").innerHTML =
      `<p class="modal-hint">불러오기 실패: ${e.message} (서버 주소 설정을 확인해주세요)</p>`;
  }
}

$("openWorksBtn").addEventListener("click", openWorks);
$("worksBackBtn").addEventListener("click", () => showView("input"));
$("studioBackBtn").addEventListener("click", openWorks);

$("worksList").addEventListener("click", async (e) => {
  const delBtn = e.target.closest(".work-card-del");
  if (delBtn) {
    e.stopPropagation();
    if (!confirm("이 작품을 삭제할까요? 되돌릴 수 없어요.")) return;
    const base = getApiBase();
    await fetch(`${base}/api/studio/${delBtn.dataset.id}`, { method: "DELETE" });
    await openWorks();
    return;
  }
  const card = e.target.closest(".work-card");
  if (!card) return;
  studioProjectId = card.dataset.id;
  saveLastOpen(studioProjectId, null);
  await loadStudio(studioProjectId);
  showView("studio");
});

$("newWorkBtn").addEventListener("click", async () => {
  // 빈 스튜디오 프로젝트를 만들고 바로 연다(스킵 흐름과 동일).
  const base = getApiBase();
  const btn = $("newWorkBtn");
  btn.disabled = true;
  try {
    const res = await fetch(`${base}/api/studio/create`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idea: "", logline: "", characters: [], key_scene: null }),
    });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    const { project_id } = await res.json();
    studioProjectId = project_id;
    saveLastOpen(studioProjectId, null);
    await loadStudio(project_id);
    showView("studio");
  } catch (e) {
    $("errorText").textContent = `요청 실패: ${e.message} (서버 주소 설정을 확인해주세요)`;
    showView("error");
  } finally {
    btn.disabled = false;
  }
});

$("startChatBtn").addEventListener("click", startChat);
$("chatSendBtn").addEventListener("click", sendChatMessage);
$("chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
});
$("finalizeBtn").addEventListener("click", finalizeChat);

$("settingsToggle").addEventListener("click", () => {
  $("settingsPanel").classList.toggle("hidden");
});
$("apiBaseInput").value = getApiBase();
$("apiBaseSave").addEventListener("click", () => {
  setApiBase($("apiBaseInput").value);
  $("settingsPanel").classList.add("hidden");
});

function resetToInput() {
  stopPolling();
  sessionId = null;
  $("ideaInput").value = "";
  $("chatMessages").innerHTML = "";
  renderOptions([]);
  showView("input");
}
$("restartBtn").addEventListener("click", resetToInput);
$("errorRetryBtn").addEventListener("click", resetToInput);

$("publishVideoBtn").addEventListener("click", async () => {
  if (!studioProjectId || !currentJobId) return;
  const btn = $("publishVideoBtn");
  btn.disabled = true;
  try {
    const base = getApiBase();
    const res = await fetch(`${base}/api/studio/${studioProjectId}/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: currentJobId, episode_num: currentEpisodeNum }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
    }
    await loadStudio(studioProjectId);
    showView("studio");
  } catch (e) {
    alert(`저장 실패: ${e.message}`);
    btn.disabled = false;
  }
});

// 새로고침 시 마지막으로 연 작품(+화)으로 자동 복귀. 실패하면(삭제된 프로젝트 등) 기록을 지우고
// 기본 화면(아이디어 입력)에 그대로 둔다 — 조용히 무시, 에러 화면으로 몰지 않는다.
(async function restoreLastOpen() {
  const last = loadLastOpen();
  if (!last || !last.projectId) return;
  try {
    studioProjectId = last.projectId;
    await loadStudio(studioProjectId);
    const hasEpisode = last.episodeNum != null &&
      (currentStudioProject.episodes || []).some((e) => e.num === last.episodeNum);
    if (hasEpisode) {
      openEpisodeDetail(last.episodeNum);
    } else {
      showView("studio");
    }
  } catch (e) {
    studioProjectId = null;
    saveLastOpen(null);
  }
})();

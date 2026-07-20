// API_BASE: 서버가 다른 origin에 있을 때(GitHub Pages/Vercel + 로컬 터널) 쓸 주소.
// localStorage에 저장해두면 재배포 없이 터널 URL이 바뀔 때마다 UI에서 바로 바꿀 수 있다.
const API_BASE_KEY = "drama_mvp_api_base";

function getApiBase() {
  return localStorage.getItem(API_BASE_KEY) || "";
}

function setApiBase(v) {
  localStorage.setItem(API_BASE_KEY, v.trim());
}

const $ = (id) => document.getElementById(id);

const views = {
  input: $("inputView"),
  chat: $("chatView"),
  pitch: $("pitchView"),
  videoPrep: $("videoPrepView"),
  studio: $("studioView"),
  characterDetail: $("characterDetailView"),
  episodeDetail: $("episodeDetailView"),
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
    if (job.status === "running") {
      updateStageList(job.stage || "진행 중");
    } else if (job.status === "done") {
      stopPolling();
      $("resultVideo").src = `${base}${job.video_url}`;
      showView("result");
    } else if (job.status === "error") {
      stopPolling();
      $("errorText").textContent = job.error || "알 수 없는 오류가 발생했어요.";
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
  $("episodeDetailScript").textContent = ep.script || "(아직 없음)";
}

function openEpisodeDetail(num) {
  currentEpisodeNum = num;
  // 편집 모드 초기화(다른 화 열 때 이전 편집 상태가 남지 않게)
  exitSubtitleEdit();
  exitSummaryEdit();
  exitScriptEdit();
  renderEpisodeDetail();
  showView("episodeDetail");
}

$("closeEpisodeDetailBtn").addEventListener("click", () => showView("studio"));

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
  const btn = $("genSummaryBtn");
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "생성 중…";
  try {
    const base = getApiBase();
    const res = await fetch(
      `${base}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}/generate-summary`,
      { method: "POST" }
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
    }
    await loadStudio(studioProjectId);
    renderEpisodeDetail();
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
  const btn = $("genScriptBtn");
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "생성 중…";
  try {
    const base = getApiBase();
    const res = await fetch(
      `${base}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}/generate-script`,
      { method: "POST" }
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
    }
    await loadStudio(studioProjectId);
    renderEpisodeDetail();
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
  const btn = $("genCharacterBtn");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "생성 중…";
  try {
    const base = getApiBase();
    // 이미 적어둔 설명이 있으면 힌트로 같이 넘겨 방향을 준다
    const hint = $("charDescriptionInput").value.trim();
    const res = await fetch(`${base}/api/studio/${studioProjectId}/characters/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, hint }),
    });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    const f = await res.json();
    // 생성 결과로 입력칸을 채운다(자동 저장 X — 사용자가 검토 후 "저장")
    $("charGenderInput").value = f.gender || "";
    $("charAgeInput").value = f.age || "";
    $("charRoleInput").value = f.role || "";
    $("charLineInput").value = f.line || "";
    $("charAppearanceInput").value = f.appearance || "";
    $("charDescriptionInput").value = f.description || "";
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
    await loadStudio(project_id);
    showView("studio");
  } catch (e) {
    $("errorText").textContent = `요청 실패: ${e.message} (서버 주소 설정을 확인해주세요)`;
    showView("error");
  } finally {
    $("goToStudioBtn").disabled = false;
  }
});

$("skipToStudioBtn").addEventListener("click", async () => {
  // 온보딩(채팅→기획안→영상 준비) 전체를 건너뛰고 빈 스튜디오 프로젝트를 바로 만든다 —
  // 로그라인·캐릭터는 스튜디오 화면에서 "+ 캐릭터 추가"·화 상세 등으로 직접 채우면 된다.
  const btn = $("skipToStudioBtn");
  btn.disabled = true;
  try {
    const base = getApiBase();
    const res = await fetch(`${base}/api/studio/create`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idea: "", logline: "", characters: [], key_scene: null }),
    });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    const { project_id } = await res.json();
    studioProjectId = project_id;
    await loadStudio(project_id);
    showView("studio");
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

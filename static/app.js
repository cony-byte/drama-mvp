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

// 등장인물 이름 집합 — 성 뗀 이름 변형도 넣는다(대본은 "이수진"을 "수진"으로 쓰는 경우가 많다).
function buildNameSet(chars) {
  const set = new Set();
  for (const c of chars || []) {
    const n = (c.name || "").trim();
    if (!n) continue;
    set.add(n);
    if (n.length >= 3) set.add(n.slice(-2)); // 이수진→수진, 강태민→태민
  }
  return set;
}
const _stripTrailingParen = (s) => s.replace(/\s*[(（][^)）]*[)）]\s*$/, "").trim();

// "이름: 대사" / "이름 (Na): 대사" 형태(콜론형 대사) 판별. 알려진 인물명이면 확실, 아니면
// 짧고 공백·문장부호 없는 이름일 때만 대사로 인정(메타/서술 줄 오검출 방지).
function matchColonDialogue(trimmed, names) {
  const idx = trimmed.search(/[:：]/);
  if (idx < 0) return null;
  const before = trimmed.slice(0, idx);
  const text = trimmed.slice(idx + 1).replace(/^\s+/, "");
  const pm = before.match(/^(.*?)\s*([(（][^)）]*[)）])\s*$/); // 이름 뒤 (Na)/(E) 등 분리
  const bare = (pm ? pm[1] : before).trim();
  const suffix = pm ? pm[2] : "";
  if (!bare) return null;
  const known = names && names.has(bare);
  const looksName = bare.length <= 10 && !/[.!?…,\s]/.test(bare);
  if (!known && !looksName) return null;
  return { name: bare, suffix, text };
}

// 공백 구분형 대사/내레이션: `이름 (Na)    대사` / `이름    대사` (콜론 없이 이름+2칸이상 공백+대사).
// 대본 스펙이 콜론이 아니라 다중 공백으로 이름과 대사를 구분하므로(★2026-07-23 *…* 감싸기 도입 후
// 이 형식이 이름 강조에 안 걸려 색·볼드가 빠지던 회귀 수정). 이름은 등록 인물이거나 이름스러운 짧은 토큰.
function matchSpaceDialogue(trimmed, names) {
  const m = trimmed.match(/^(.{1,12}?)\s*([(（][^)）]*[)）])?\s{2,}(\S.*)$/);
  if (!m) return null;
  const bare = m[1].trim();
  const suffix = m[2] || "";
  if (!bare) return null;
  const known = names && names.has(bare);
  const looksName = bare.length <= 10 && !/[.!?…,()（）\s]/.test(bare);
  if (!known && !looksName) return null;
  return { name: bare, suffix, text: m[3] };
}

// 대사 한 줄 HTML — 인물명(강조) + 대사. 이름 옆 (Na)/(E) 접미는 이름과 함께 강조 처리하고,
// 대사 본문 안의 (지문)만 muted 처리한다.
function dialogueHtml(name, suffix, rawText) {
  const label = suffix ? `${escapeHtml(name)} ${escapeHtml(suffix)}` : escapeHtml(name);
  const body = renderInlineMarkdown(rawText)
    .replace(/([(（])([^)）]*)([)）])/g, '<span class="dialogue-cue">$1$2$3</span>');
  return `<div class="script-dialogue">`
    + `<span class="dialogue-char">${label}</span>`
    + `<span class="dialogue-text">${body}</span></div>`;
}

// *…* / **…** 로 감싼 한 줄을 벗겨 안쪽만 반환(감싼 게 아니면 그대로).
function _unwrapStars(t) {
  const m = t.match(/^(\*{1,2})([^\n]+?)\1$/);
  return m ? m[2].trim() : t;
}

// 대본 렌더 — 지문(서술/괄호)과 대사(콜론형·이름헤더형)를 구분한다. names(Set)를 주면 이름-헤더형
// 대사("이름"만 있는 줄 다음이 대사)까지 잡는다. 인물+대사를 *…*로 감싼 표기도 대사로 인식한다.
function renderScriptMarkdown(text, names) {
  const lines = String(text || "(아직 없음)").split(/\r?\n/);
  const firstContent = lines.findIndex((line) => line.trim());
  const out = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();
    if (!trimmed) { out.push('<div class="script-spacer" aria-hidden="true"></div>'); continue; }
    if (/^-{3,}$/.test(trimmed)) { out.push('<hr class="script-divider">'); continue; }

    // *…*/**…** 로 감싼 줄도 벗겨서 판정(인물+대사를 *로 감싸 강조하는 표기 지원)
    const core = _unwrapStars(trimmed);

    // 대사(콜론형) — 감싼 것도 인식
    const cd = matchColonDialogue(core, names);
    if (cd) { out.push(dialogueHtml(cd.name, cd.suffix, cd.text)); continue; }

    // 지문: 통째로 괄호로 감싼 줄
    if (/^[(（].*[)）]$/.test(core)) {
      out.push(`<div class="script-direction">${renderInlineMarkdown(core)}</div>`);
      continue;
    }

    // 대사(공백 구분형): `이름 (Na)    대사` — 콜론 없이 이름+다중공백+대사(감싼 것도 core로 인식)
    const sd = matchSpaceDialogue(core, names);
    if (sd) { out.push(dialogueHtml(sd.name, sd.suffix, sd.text)); continue; }

    // 제목/씬 헤딩(마크다운) — 원문 기준
    const heading = trimmed.match(/^#{1,3}\s+(.+)$/);
    const singleStarTitle = trimmed.match(/^\*([^*].*?)\*$/);
    const boldLine = trimmed.match(/^\*\*(.+?)\*\*$/);
    const emphasizedTitle = i === firstContent
      && (singleStarTitle || (boldLine && !/^\d+\./.test(boldLine[1].trim())));
    if (heading || emphasizedTitle) {
      out.push(`<h4 class="script-title">${renderInlineMarkdown((heading || emphasizedTitle)[1])}</h4>`);
      continue;
    }
    if (boldLine) {
      out.push(`<div class="script-scene-heading">${renderInlineMarkdown(boldLine[1])}</div>`);
      continue;
    }

    // 대사(이름-헤더형): "이름"만 있는 줄 → 뒤따르는 줄들이 그 인물의 대사(빈 줄·지문·다음 화자 전까지)
    if (names) {
      const bare = _stripTrailingParen(core);
      const sufMatch = core.match(/([(（][^)）]*[)）])\s*$/);
      if (names.has(bare)) {
        const speech = [];
        let j = i + 1;
        while (j < lines.length) {
          const t2 = _unwrapStars(lines[j].trim());
          if (!t2 || /^[(（].*[)）]$/.test(t2) || matchColonDialogue(t2, names) || names.has(_stripTrailingParen(t2))) break;
          speech.push(lines[j].trim());
          j++;
        }
        if (speech.length) {
          out.push(dialogueHtml(bare, sufMatch ? sufMatch[1] : "", speech.join("\n")));
          i = j - 1;
          continue;
        }
      }
    }

    out.push(`<div class="script-line">${renderInlineMarkdown(line)}</div>`);
  }
  return out.join("");
}

// ── 대본을 씬 단위로 분할/직렬화 (프론트 전용 뷰 — episode.script 원본 텍스트는 그대로 두고
//    화면에서만 씬 카드로 쪼갠다. 세그먼트 text를 "\n"으로 다시 이어붙이면 원본과 정확히
//    일치하므로(연속 슬라이스) 저장 시 다운스트림 파서를 깨지 않는다). ──
function isSceneHeaderLine(line) {
  const t = line.trim();
  if (!t) return false;
  if (/^\*{1,2}\s*\d+\s*[.)]\s*.+\*{1,2}$/.test(t)) return true;    // *1. [...]*  /  **1) ...**
  if (/^\d+\s*[.)]\s+\S/.test(t)) return true;                       // 1. 편의점 / 밤 11시
  if (/^(■\s*)?(씬|장면|scene)\s*#?\s*\d+/i.test(t)) return true;     // 씬3 / SCENE 3 / ■ 씬3
  return false;
}

function parseScriptSegments(text) {
  const lines = String(text || "").split("\n");
  const headers = [];
  lines.forEach((l, i) => { if (isSceneHeaderLine(l)) headers.push(i); });
  const segs = [];
  if (!headers.length) {
    segs.push({ type: "scene", text: lines.join("\n"), num: 1, header: "" });
    return segs;
  }
  if (headers[0] > 0) {
    segs.push({ type: "preamble", text: lines.slice(0, headers[0]).join("\n"), header: "" });
  }
  headers.forEach((start, k) => {
    const end = k + 1 < headers.length ? headers[k + 1] : lines.length;
    segs.push({ type: "scene", text: lines.slice(start, end).join("\n"), num: k + 1, header: lines[start].trim() });
  });
  return segs;
}

function serializeSegments(segs) {
  return segs.map((s) => s.text).join("\n");
}

// 씬 헤더를 prefix(마크·번호·"씬N") + title(로케이션/시간) + suffix(닫는 마크)로 분해.
// 헤더를 편집할 때 title만 갈아끼우고 prefix/suffix를 붙여 원본 포맷(*1. …*)을 보존한다.
function splitSceneHeader(header) {
  let s = String(header || ""), prefix = "", suffix = "";
  const lead = s.match(/^\*{1,2}/); if (lead) { prefix = lead[0]; s = s.slice(lead[0].length); }
  const trail = s.match(/\*{1,2}$/); if (trail) { suffix = trail[0]; s = s.slice(0, s.length - trail[0].length); }
  const num = s.match(/^\s*\d+\s*[.)]\s*/); if (num) { prefix += num[0]; s = s.slice(num[0].length); }
  const sc = s.match(/^(■\s*)?(씬|장면|scene)\s*#?\s*\d+\s*[.)\-]?\s*/i); if (sc) { prefix += sc[0]; s = s.slice(sc[0].length); }
  return { prefix, title: s.trim(), suffix };
}
function sceneHeaderTitle(header) { return splitSceneHeader(header).title; }

// 편집(자동저장)이 인덱스로 참조하는 씬 세그먼트 모델. renderScriptScenes가 매 렌더마다 갱신하고,
// saveSceneEdit는 재파싱 대신 이 모델을 수정→직렬화한다(편집 중 카드 재렌더 없이 인덱스 안정).
let scriptSegsModel = [];

function renderScriptScenes(text, locked, names) {
  const segs = parseScriptSegments(text);
  scriptSegsModel = segs;
  const hasContent = segs.some((s) => s.text.trim());
  if (!hasContent && locked) return '<div class="script-empty">(아직 없음)</div>';
  return segs.map((s, i) => {
    // 타이틀/도입부(preamble)는 카드로 표시하지 않는다(원문에는 그대로 남아 라운드트립엔 영향 없음).
    // data-seg 인덱스가 어긋나지 않도록 i는 그대로 두고 빈 문자열만 반환.
    if (s.type !== "scene") return "";
    const label = `씬 ${s.num}`;
    const title = sceneHeaderTitle(s.header);
    // 헤더 줄이 있으면 카드 헤더에서 보여주므로 본문에선 첫 줄(헤더) 제외
    const bodyText = s.header ? s.text.split("\n").slice(1).join("\n") : s.text;
    // 헤더(로케이션/시간): 확정=읽기전용, 미확정+헤더있음=바로 편집 input(원본 *N.…* 포맷은 저장 시 보존)
    const headerNode = (!locked && s.header)
      ? `<input class="scene-header-input" data-seg="${i}" value="${escapeHtml(title)}" placeholder="로케이션 / 시간" spellcheck="false">`
      : `<span class="scene-title">${escapeHtml(title)}</span>`;
    // 확정(잠금)=대사/지문 스타일 읽기전용 · 미확정(기본)=본문을 바로 편집하는 textarea(버튼 없이 자동저장)
    let body;
    if (locked) {
      body = renderScriptMarkdown(bodyText, names);
    } else {
      const rows = Math.min(40, Math.max(3, bodyText.split("\n").length));
      body = `<textarea class="scene-edit-area" data-seg="${i}" rows="${rows}" spellcheck="false"`
        + ` placeholder="대본을 입력하거나 🤖 AI 생성을 사용하세요">${escapeHtml(bodyText)}</textarea>`;
    }
    return `<div class="scene-card" data-seg="${i}">
        <div class="scene-card-head">
          <button type="button" class="scene-collapse-btn" data-seg="${i}" aria-expanded="true">
            <span class="scene-caret" aria-hidden="true">▾</span>
            <span class="scene-num">${label}</span>
          </button>
          ${headerNode}
        </div>
        <div class="scene-card-body">${body}</div>
      </div>`;
  }).join("");
}

// "AI 생성" 버튼 첫 클릭 = 의견 입력창(툴팁 박스)만 펼치고 대기, 이미 펼쳐진 상태에서 클릭(또는
// 툴팁 안 "생성" 버튼) = 그 값으로 진행. 툴팁 박스는 textarea를 감싼 `${id}Box` — 없으면(구
// 마크업 호환) textarea 자체를 박스로 취급한다.
function _noteBox(noteInputId) {
  return $(noteInputId + "Box") || $(noteInputId);
}

// "🤖 AI 생성" 진입점. 툴팁이 닫혀 있으면 열고 대기(null 반환 — 호출자는 그냥 return).
// 이미 열려 있으면(두 번째 클릭 또는 툴팁 안 "생성") 의견값을 읽어 반환하면서 ★툴팁을 즉시 닫는다★
// (값은 반환값으로 넘어가니 입력칸을 비워도 안전). 이후 호출자가 버튼을 "생성 중…"으로 바꾼다.
function beginAiGen(noteInputId) {
  const box = _noteBox(noteInputId);
  const input = $(noteInputId);
  if (box.classList.contains("hidden")) {
    box.classList.remove("hidden");
    input.focus();
    return null;
  }
  const note = input.value.trim();
  input.value = "";
  box.classList.add("hidden");
  return note;
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
    // beginAiGen이 의견값을 반환하며 툴팁을 닫고 그대로 생성으로 진행된다(중복 구현 방지).
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
        // 생성 중엔 생성 중인(최신) 씬을 따라가되, 유저가 이전 씬으로 넘겨봤으면(stillsFollow=false) 그대로 둔다.
        if (job.status !== "done" && stillsFollow) {
          stillsSceneIndex = _stillsSceneNums().length - 1;   // 생성 중인 최신 씬
          const sn = _stillsSceneNums()[stillsSceneIndex];
          stillsCutIndex = Math.max(0, stillsCuts.filter((c) => Number(c.scene_num) === Number(sn)).length - 1); // 최신 컷
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
  // ★2026-07-23(온보딩 B): 기획 확정(finalize) 시 서버가 작품을 만들고 project_id를 실어 준다.
  // 그 id를 잡아두면 '스튜디오로 이동'이 새로 만들지 않고 이 작품으로 진입/갱신한다(재생성도 동일 id).
  if (card && card.project_id) studioProjectId = card.project_id;
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
  const note = beginAiGen("synopsisNoteInput");
  if (note === null) return;
  const btn = $("genSynopsisBtn");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "생성 중…";
  try {
    const base = getApiBase();
    const res = await fetch(`${base}/api/studio/${studioProjectId}/generate-synopsis`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `서버 응답 오류 (${res.status})`);
    }
    renderStudio(await res.json());
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
  const locked = isScriptConfirmed();
  const names = buildNameSet(currentStudioProject.characters);
  $("episodeDetailScript").innerHTML = renderScriptScenes(ep.script, locked, names);
  applyScriptLock(locked);
}

function openEpisodeDetail(num) {
  currentEpisodeNum = num;
  stillsPageIndex = 0; stillsSceneIndex = 0; stillsCutIndex = 0; stillsFollow = true; // 다른 화 열 때 스틸 페이지 처음으로
  saveLastOpen(studioProjectId, num);
  // 편집 모드 초기화(다른 화 열 때 이전 편집 상태가 남지 않게)
  exitSubtitleEdit();
  exitSummaryEdit();
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
        stillsPageIndex = 0; stillsSceneIndex = 0; stillsCutIndex = 0; stillsFollow = true;  // 새 생성 시작 → 생성 중 씬 자동 추적
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

// ★2026-07-23: "+ 다음 씬 만들기" 버튼 제거 — 씬 페이지네이션의 "+" 빈 카드가 그 역할을 대신한다.

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
let stillsSceneIndex = 0;   // ★2026-07-23 씬 단위 페이지네이션: 현재 보고 있는 씬 페이지(0-based)
let stillsFollow = true;    // 생성 중 최신(생성 중) 씬을 자동 추적. 유저가 이전/다음으로 넘기면 false → 안 뺏김
let stillsCutIndex = 0;     // 현재 씬 안에서 보고 있는 컷(스틸) 인덱스 — 씬 안은 예전처럼 한 장씩 페이지네이션
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
  // ★2026-07-23: per-컷 [영상화] 제거 — 영상화는 스틸 검수 후 [전체 영상화] 한 번으로만.
  const media = c.image ? `<img src="${c.image}" alt="씬${c.scene_num} 컷${c.cut_num}">` : "";
  div.innerHTML = `
    <div class="still-media">${media}</div>
    <div class="still-title">씬${c.scene_num} · 컷${c.cut_num}</div>
    <div class="still-caption">${c.caption || ""}</div>
    <div class="still-cut-actions">
      <button type="button" class="text-btn cut-regen-btn">🔁 재생성</button>
      <button type="button" class="text-btn cut-delete-btn">🗑️ 삭제</button>
    </div>`;
  return div;
}

// 스틸이 아직 하나도 안 나온 로딩 상태 — 백엔드 파이프라인 현재 단계(stage)를 "생성 중…"으로.
function renderStillsLoading(stage) {
  renderScenePager();   // 생성(로딩) 중에도 씬 페이지네이션은 위에 그대로 유지
  $("stillsList").innerHTML = `<div class="roster-empty">🎬 ${escapeHtml(stage || "생성 중…")}</div>`;
}

// 씬 페이지네이션(상단 고정) 클릭 — 씬 이동. 씬을 바꾸면 그 씬의 첫 컷부터, 폴링 자동추적 해제.
$("stillsScenePager").addEventListener("click", (e) => {
  if (e.target.closest(".scene-prev-btn")) {
    stillsFollow = false;
    if (stillsSceneIndex > 0) { stillsSceneIndex--; stillsCutIndex = 0; renderStillsPage(); }
  } else if (e.target.closest(".scene-next-btn")) {
    stillsFollow = false;
    if (stillsSceneIndex < _stillsSceneNums().length - 1) { stillsSceneIndex++; stillsCutIndex = 0; renderStillsPage(); }
  }
});

// ★2026-07-23 씬 단위 페이지네이션: 전체 씬 목록(scene_lines) 기준으로 한 페이지=한 씬.
//   그 씬의 컷들을 카드로 나열하고, 아직 스틸이 없는 씬이면 "+" 빈 카드 1장(클릭→그 씬 생성).
//   다음 씬 생성 중에도 이전 씬을 자유롭게 넘겨볼 수 있고(폴링이 강제로 페이지를 안 옮김), job은 안 끊긴다.
function _stillsSceneNums() {
  const ep = currentEpisode();
  const lines = (ep && ep.scene_lines) || [];
  if (lines.length) return lines.map((l) => Number(l[0]));
  // scene_lines가 아직 없으면 만들어진 스틸의 씬 번호로 대체
  return [...new Set(stillsCuts.map((c) => Number(c.scene_num)))].sort((a, b) => a - b);
}
function _sceneTitle(sceneNum) {
  const ep = currentEpisode();
  const hit = ((ep && ep.scene_lines) || []).find((l) => Number(l[0]) === Number(sceneNum));
  return hit ? (hit[1] || "") : "";
}

// ★2026-07-23: 씬 페이지네이션 바(상단 고정) — #stillsList 밖의 별도 영역이라 생성 중 로딩으로
//   #stillsList가 갈려도 사라지지 않는다("빈 카드 눌러 생성 시 씬 페이지네이션 사라짐" 수정).
function renderScenePager() {
  const box = $("stillsScenePager");
  if (!box) return;
  const sceneNums = _stillsSceneNums();
  if (!sceneNums.length) { box.innerHTML = ""; return; }
  stillsSceneIndex = Math.max(0, Math.min(stillsSceneIndex, sceneNums.length - 1));
  const sceneNum = sceneNums[stillsSceneIndex];
  const title = _sceneTitle(sceneNum);
  box.innerHTML = `
    <button type="button" class="text-btn scene-prev-btn"${stillsSceneIndex === 0 ? " disabled" : ""}>◀</button>
    <span class="stills-counter">씬 ${stillsSceneIndex + 1} / ${sceneNums.length}${title ? " · " + escapeHtml(title) : ""}</span>
    <button type="button" class="text-btn scene-next-btn"${stillsSceneIndex === sceneNums.length - 1 ? " disabled" : ""}>▶</button>`;
}

// #stillsList: 현재 씬의 컷들을 예전처럼 '한 장씩'(◀이전 N/M 다음▶) 보여준다. 스틸 없는 씬은 "+" 카드.
function renderStillsPage() {
  renderScenePager();
  const list = $("stillsList");
  list.innerHTML = "";
  const sceneNums = _stillsSceneNums();
  if (!sceneNums.length) return;
  const sceneNum = sceneNums[stillsSceneIndex];
  const cuts = stillsCuts.filter((c) => Number(c.scene_num) === Number(sceneNum));
  if (!cuts.length) {
    // 스틸 없는 씬 → "+" 빈 카드 1장(클릭→그 씬 생성). 씬 페이지네이션은 위에 그대로 유지된다.
    const mk = document.createElement("button");
    mk.type = "button";
    mk.className = "still-card make-scene-card";
    mk.dataset.scene = sceneNum;
    mk.innerHTML = `<div class="still-media make-scene-plus">＋</div>
      <div class="still-title">씬${sceneNum} 스틸컷 만들기</div>`;
    list.appendChild(mk);
    return;
  }
  stillsCutIndex = Math.max(0, Math.min(stillsCutIndex, cuts.length - 1));
  list.appendChild(_stillCardEl(cuts[stillsCutIndex]));
  const pager = document.createElement("div");
  pager.className = "stills-pager";
  pager.innerHTML = `
    <button type="button" class="text-btn stills-prev-btn"${stillsCutIndex === 0 ? " disabled" : ""}>◀ 이전</button>
    <span class="stills-counter">${stillsCutIndex + 1} / ${cuts.length}</span>
    <button type="button" class="text-btn stills-next-btn"${stillsCutIndex === cuts.length - 1 ? " disabled" : ""}>다음 ▶</button>`;
  list.appendChild(pager);
}

function renderStillsList(items, total) {
  const list = $("stillsList");
  list.innerHTML = "";
  // v3.1 스틸은 클립마다 한 장씩(각자 clip_id 보유) — 전부 보여준다. 구 파이프라인만 씬당 대표 1장.
  const isV3 = (items || []).some((it) => it.clip_id != null);
  stillsCuts = (isV3 ? [...(items || [])] : representativePreviewItems(items)).sort((a, b) =>
    (a.scene_num - b.scene_num) || ((a.cut_num || 0) - (b.cut_num || 0)));
  const sceneNums = _stillsSceneNums();
  if (!sceneNums.length) {
    // 씬 목록도 스틸도 없음: 생성 중이면 로딩, 아니면 "씬1부터 만들기" 카드로 진입점 제공.
    if (total) { list.innerHTML = `<div class="roster-empty">생성 중...</div>`; return; }
    const mk = document.createElement("button");
    mk.type = "button";
    mk.className = "still-card make-scene-card";
    mk.dataset.scene = 1;
    mk.innerHTML = `<div class="still-media make-scene-plus">＋</div>
      <div class="still-title">씬1부터 스틸컷 만들기</div>`;
    list.appendChild(mk);
    return;
  }
  renderStillsPage();  // 씬 단위 페이지(그 씬 컷들 or "+" 빈 카드)
}

function renderStills() {
  const ep = currentEpisode();
  const stills = (ep && ep.scene_stills) || [];
  renderStillsList(stills, stills.length);   // 다음 씬 버튼 제거 — 씬 페이지네이션의 "+" 빈 카드로 대체
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
  // 컷 페이지네이션(◀ 이전 / 다음 ▶) — 현재 씬 '안'에서 컷 한 장씩 넘긴다.
  if (e.target.closest(".stills-prev-btn")) {
    stillsFollow = false;
    if (stillsCutIndex > 0) { stillsCutIndex--; renderStillsPage(); }
    return;
  }
  if (e.target.closest(".stills-next-btn")) {
    stillsFollow = false;
    const sn = _stillsSceneNums()[stillsSceneIndex];
    const n = stillsCuts.filter((c) => Number(c.scene_num) === Number(sn)).length;
    if (stillsCutIndex < n - 1) { stillsCutIndex++; renderStillsPage(); }
    return;
  }

  const card = e.target.closest(".still-card");
  if (!card || !studioProjectId || !currentEpisodeNum) return;
  const sceneNum = card.dataset.scene;
  const cutNum = card.dataset.cut;
  const base = getApiBase();

  // "+" 빈 카드 클릭 → 그 씬(dataset.scene) 스틸 생성. v3로 만들어진 화면 v3 경로로.
  if (card.classList.contains("make-scene-card")) {
    const ep = currentEpisode();
    const isV3 = v3Mode || !!(ep && ep.v3_scenes && ep.v3_scenes.length);
    const endpoint = isV3 ? "v3/preview-scene" : "preview-stills";
    const gen = Number(card.dataset.scene) || 1;
    _setV3Buttons(isV3);
    try {
      await startJob(endpoint, "stills", `씬${gen} 준비 중`, { scene_num: gen });
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

  // ★2026-07-23: per-컷 영상화 핸들러 제거(전체 영상화만 사용).

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
  const note = beginAiGen("summaryNoteInput");
  if (note === null) return;
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
        body: JSON.stringify({ note }),
      }
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

// ── 대본 (AI 생성) — 수정은 씬 카드에서 바로(버튼 없이 자동저장), 별도 편집 버튼 없음 ──
$("genScriptBtn").addEventListener("click", async () => {
  const note = beginAiGen("scriptNoteInput");
  if (note === null) return;
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
        body: JSON.stringify({ note }),
      }
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

// ── 대본 씬 카드: 접기/펴기 + 본문·헤더 바로 편집(자동저장) ──
// 값이 바뀐 채 포커스가 빠지면(change) 자동 저장한다. 저장은 서버 PATCH + currentStudioProject
// 갱신까지만 하고 ★대본 카드는 재렌더하지 않아★ 편집 흐름(펼침/커서/다른 씬 입력)을 보존한다.
async function persistScript(flashEl) {
  const base = getApiBase();
  const res = await fetch(
    `${base}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}`,
    { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ script: serializeSegments(scriptSegsModel) }) }
  );
  if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
  await loadStudio(studioProjectId);   // 원문 갱신(재렌더는 안 함)
  flashSaved(flashEl);
}

// 본문 편집 저장 — 헤더 줄은 그대로 두고 본문만 교체
async function saveSceneEdit(ta) {
  const seg = scriptSegsModel[Number(ta.dataset.seg)];
  if (!seg) return;
  const origBody = seg.header ? seg.text.split("\n").slice(1).join("\n") : seg.text;
  if (ta.value === origBody) return;   // 변경 없음
  seg.text = seg.header ? seg.text.split("\n")[0] + "\n" + ta.value : ta.value;
  try { await persistScript(ta); } catch (e) { alert(`대본 저장 실패: ${e.message}`); }
}

// 헤더(로케이션/시간) 편집 저장 — title만 갈아끼우고 원본 prefix/suffix(*N.…* 등)는 보존
async function saveSceneHeader(input) {
  const seg = scriptSegsModel[Number(input.dataset.seg)];
  if (!seg || !seg.header) return;
  const parts = seg.text.split("\n");
  const cur = splitSceneHeader(parts[0]);
  const newTitle = input.value.trim();
  if (newTitle === cur.title) return;   // 변경 없음
  parts[0] = cur.prefix + newTitle + cur.suffix;
  seg.text = parts.join("\n");
  seg.header = parts[0].trim();
  try { await persistScript(input); } catch (e) { alert(`씬 헤더 저장 실패: ${e.message}`); }
}

function flashSaved(el) {
  const card = el.closest(".scene-card");
  if (!card) return;
  card.classList.add("scene-saved");
  setTimeout(() => card.classList.remove("scene-saved"), 1200);
}

$("episodeDetailScript").addEventListener("click", (e) => {
  const collapseBtn = e.target.closest(".scene-collapse-btn");
  if (!collapseBtn) return;
  const card = collapseBtn.closest(".scene-card");
  const collapsed = card.classList.toggle("collapsed");
  collapseBtn.setAttribute("aria-expanded", String(!collapsed));
});
$("episodeDetailScript").addEventListener("change", (e) => {
  const ta = e.target.closest(".scene-edit-area");
  if (ta) { saveSceneEdit(ta); return; }
  const hi = e.target.closest(".scene-header-input");
  if (hi) saveSceneHeader(hi);
});

// ── 대본 확정(잠금): "이 대본으로 확정" → 편집 잠금(분량 측정 기준). 상태는 브라우저 로컬에
//    화별로 저장(백엔드 미변경 = 서버 리로드/job 영향 없음). "해제"로 되돌릴 수 있다. ──
function scriptLockKey() { return `drama:scriptConfirmed:${studioProjectId}:${currentEpisodeNum}`; }
function isScriptConfirmed() { return localStorage.getItem(scriptLockKey()) === "1"; }
function setScriptConfirmed(v) {
  if (v) localStorage.setItem(scriptLockKey(), "1");
  else localStorage.removeItem(scriptLockKey());
}

function applyScriptLock(locked) {
  if (typeof locked === "undefined") locked = isScriptConfirmed();
  // 확정 상태면 AI 생성 숨김(재생성 차단). 씬 편집칸은 renderScriptScenes가 locked면 읽기전용으로 렌더.
  $("genScriptBtn").classList.toggle("hidden", locked);
  $("addSceneBtn").classList.toggle("hidden", locked);   // 확정 상태면 씬 추가 숨김(편집 중에만)
  if (locked) hideNote("scriptNoteInput");
  // 확정 버튼 ↔ 확정됨 표시/해제 토글
  $("confirmScriptBtn").classList.toggle("hidden", locked);
  $("scriptConfirmedNote").classList.toggle("hidden", !locked);
  $("unlockScriptBtn").classList.toggle("hidden", !locked);
  $("episodeDetailScript").classList.toggle("locked", locked);
  // ★[드라마 만들기]는 확정(locked) 후에만 노출 — 확정 전엔 숨김. 확정하면 [수정]+[드라마 만들기]로 나뉨.
  $("v3PreviewBtn").classList.toggle("hidden", !locked);
  if (locked) gateHide();  // 확정 완료 상태면 게이트 챗봇 정리(측정 OK 버블 등)
}

// [확정] → 분량 측정(measure-duration). 범위 안이면 자동 확정, 벗어나면 압축/분할/확장 제안.
$("confirmScriptBtn").addEventListener("click", runDurationGate);
$("unlockScriptBtn").addEventListener("click", () => {
  setScriptConfirmed(false);
  renderEpisodeDetail();
});

// ＋ 씬 추가 — 대본 끝에 새 씬 카드(헤더 + 빈 본문)를 붙이고 저장 후 재렌더.
$("addSceneBtn").addEventListener("click", async () => {
  const nextNum = (scriptSegsModel || []).filter((s) => s.type === "scene").length + 1;
  const header = `${nextNum}. 새 장소 / 시간`;
  scriptSegsModel.push({ type: "scene", text: header + "\n", num: nextNum, header });
  const btn = $("addSceneBtn");
  btn.disabled = true;
  try {
    await patchEpisode({ script: serializeSegments(scriptSegsModel) });  // PATCH + loadStudio + 재렌더
  } catch (e) {
    alert(`씬 추가 실패: ${e.message}`);
  } finally {
    btn.disabled = false;
  }
});

// ── 분량 게이트: [확정] → 스켈레톤으로 화 러닝타임 측정(90~120초). 벗어나면 AI 자동맞춤 제안. ──
//    측정=measure-duration(스켈레톤 LLM 1회), 자동맞춤=autofit-duration(compress|expand|split).
function gateChatBox() { return $("durationGateChat"); }
function gateShow(html) { const b = gateChatBox(); b.classList.remove("hidden"); b.innerHTML = html; }
function gateHide() { const b = gateChatBox(); b.classList.add("hidden"); b.innerHTML = ""; }

async function gatePost(path, body) {
  const base = getApiBase();   // ★2026-07-23 버그픽스: 이 줄이 없어 브라우저에서 ReferenceError→"측정 실패"였음
  const r = await fetch(`${base}/api/studio/${studioProjectId}/episodes/${currentEpisodeNum}/${path}`,
    { method: "POST", headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : null });
  if (!r.ok) throw new Error(((await r.json().catch(() => ({}))).detail) || "요청 실패");
  return r.json();
}

function gateBubble(msg, buttons) {
  const btns = (buttons || []).map(b =>
    `<button type="button" class="gate-btn ${b.primary ? "primary" : ""}" data-act="${b.act}">${b.label}</button>`).join("");
  return `<div class="gate-bubble"><p>${msg}</p>${btns ? `<div class="gate-actions">${btns}</div>` : ""}</div>`;
}

function gateScenes(m) { return (m.scenes || []).map(s => `씬${s.num} ${s.seconds}초`).join(" · "); }

async function runDurationGate() {
  const btn = $("confirmScriptBtn");
  if (btn.disabled) return;              // 측정 중 중복 클릭 방지(느린 터널에서 요청 쌓임 → 실패 방지)
  btn.disabled = true;
  gateShow(gateBubble("⏳ 대본으로 분량을 재는 중… (처음엔 뼈대 생성으로 최대 1분, 이후엔 즉시)"));
  try {
    renderGateVerdict(await gatePost("measure-duration"));
  } catch (e) {
    gateShow(gateBubble("분량 측정에 실패했어요 😢 잠시 후 [이 대본으로 확정]을 다시 눌러줘요.",
      [{ act: "close", label: "닫기" }]));
  } finally {
    btn.disabled = false;
  }
}

function renderGateVerdict(m) {
  if (m.verdict === "ok") {
    gateShow(gateBubble(`✅ 분량 약 <b>${m.total}초</b> — 권장 ${m.min}~${m.max}초 안에 들어와요. 이 대본으로 확정할게요!`));
    setScriptConfirmed(true);
    setTimeout(() => { gateHide(); renderEpisodeDetail(); }, 1200);
    return;
  }
  if (m.verdict === "over") {
    gateShow(gateBubble(
      `지금 대본은 약 <b>${m.total}초</b>예요 (권장 ${m.min}~${m.max}초보다 길어요).<br><small>${gateScenes(m)}</small><br>제가 약 ${m.target}초로 <b>압축</b>해드릴까요?`,
      [{ act: "compress", label: "👌 압축해줘", primary: true }, { act: "confirm-anyway", label: "그냥 이대로 확정" }]));
  } else {
    gateShow(gateBubble(
      `지금 대본은 약 <b>${m.total}초</b>로 좀 짧아요 (권장 ${m.min}~${m.max}초).<br><small>${gateScenes(m)}</small><br>제가 약 ${m.target}초로 <b>늘려</b>드릴까요?`,
      [{ act: "expand", label: "👌 늘려줘", primary: true }, { act: "confirm-anyway", label: "그냥 이대로 확정" }]));
  }
}

async function gateAutofit(mode) {
  gateShow(gateBubble(mode === "split" ? "⏳ 두 화로 나누는 중…"
    : mode === "expand" ? "⏳ 대본을 늘리는 중…" : "⏳ 대본을 압축하는 중…"));
  try {
    const res = await gatePost("autofit-duration", { mode });
    await loadStudio(studioProjectId);   // 조정된 대본을 에디터에 반영
    const m = res.measure;
    if (m.verdict === "ok") {
      gateShow(gateBubble(`✨ 약 <b>${m.total}초</b>로 맞췄어요! 대본을 업데이트했어요. 이대로 확정할까요?`,
        [{ act: "confirm-now", label: "✅ 확정", primary: true }, { act: "close", label: "조금 더 볼게요" }]));
    } else if (m.verdict === "over") {
      gateShow(gateBubble(`압축해도 약 <b>${m.total}초</b>라 한 화(${m.min}~${m.max}초)엔 좀 길어요.<br>두 화로 <b>나눠서</b> 1화만 쓸까요?`,
        [{ act: "split", label: "✂️ 나눠줘", primary: true }, { act: "confirm-now", label: "그냥 이대로 확정" }]));
    } else {
      gateShow(gateBubble(`조정 후 약 <b>${m.total}초</b>예요. 한 번 더 맞춰볼까요?`,
        [{ act: "expand", label: "🔁 더 늘려줘", primary: true }, { act: "confirm-now", label: "✅ 이대로 확정" }]));
    }
  } catch (e) {
    gateShow(gateBubble("조정에 실패했어요 😢 다시 시도해줄래요?", [{ act: "close", label: "닫기" }]));
  }
}

$("durationGateChat").addEventListener("click", (e) => {
  const b = e.target.closest(".gate-btn"); if (!b) return;
  const act = b.dataset.act;
  if (act === "close") return gateHide();
  if (act === "confirm-anyway" || act === "confirm-now") {
    setScriptConfirmed(true); gateHide(); renderEpisodeDetail(); return;
  }
  if (act === "compress" || act === "expand" || act === "split") return gateAutofit(act);
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
  const note = beginAiGen("charHintInput");
  if (note === null) return;
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
        hint: note,
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
    // ★2026-07-23(온보딩 B): 작품은 기획 확정(finalize) 때 이미 생성됨. 여기선 새로 만들지 않고,
    // 화면에서 편집·생성된 편집분(로그라인·인물 이미지·키장면)만 그 작품에 PATCH로 반영하고 진입한다.
    // (studioProjectId가 없으면 예외적 폴백으로 생성 — finalize를 안 거친 진입 등)
    if (studioProjectId) {
      await fetch(`${base}/api/studio/${studioProjectId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          logline: currentCard.logline,
          characters: currentCard.characters || [],
          key_scene: currentCard.key_scene || null,
        }),
      });
    } else {
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
      studioProjectId = (await res.json()).project_id;
    }
    saveLastOpen(studioProjectId, null);
    await loadStudio(studioProjectId);
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
$("studioWorksBtnBottom").addEventListener("click", openWorks);  // 스튜디오 하단 "내 작품"

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

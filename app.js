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

async function startChat() {
  const idea = $("ideaInput").value.trim();
  if (!idea) return;
  const base = getApiBase();
  $("startChatBtn").disabled = true;
  try {
    showView("chat");
    $("chatMessages").innerHTML = "";
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
  } catch (e) {
    pending.classList.remove("pending");
    pending.textContent = `(응답 실패: ${e.message})`;
  } finally {
    $("chatSendBtn").disabled = false;
  }
}

// #stageList의 data-key가 job.stage 문자열에 포함되면 그 단계까지 진행된 것으로 본다.
const STAGE_ORDER = ["기획안", "대본", "씬 설계", "콘티", "샷 분해", "영상 제작", "합본"];

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

async function finalizeChat() {
  if (!sessionId) return;
  const base = getApiBase();
  $("finalizeBtn").disabled = true;
  try {
    const res = await fetch(`${base}/api/chat/${sessionId}/finalize`, { method: "POST" });
    if (!res.ok) throw new Error(`서버 응답 오류 (${res.status})`);
    const { job_id } = await res.json();
    updateStageList("대기 중");
    showView("progress");
    pollTimer = setInterval(() => pollJob(job_id), 3000);
    pollJob(job_id);
  } catch (e) {
    $("errorText").textContent = `요청 실패: ${e.message} (서버 주소 설정을 확인해주세요)`;
    showView("error");
  } finally {
    $("finalizeBtn").disabled = false;
  }
}

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
  showView("input");
}
$("restartBtn").addEventListener("click", resetToInput);
$("errorRetryBtn").addEventListener("click", resetToInput);

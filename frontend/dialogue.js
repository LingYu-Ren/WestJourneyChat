import { topRolesData } from "./data/top_roles.js";

const state = {
  pending: false,
  history: [],
};

const elements = {
  backToIndexLink: document.querySelector("#backToIndexLink"),
  roleName: document.querySelector("#dialogueRoleName"),
  roleAliases: document.querySelector("#dialogueRoleAliases"),
  roleSummary: document.querySelector("#dialogueRoleSummary"),
  summaryProgressThumb: document.querySelector("#dialogueSummaryProgressThumb"),
  traits: document.querySelector("#dialogueTraits"),
  hints: document.querySelector("#dialogueHints"),
  messages: document.querySelector("#chatMessages"),
  form: document.querySelector("#chatForm"),
  input: document.querySelector("#chatInput"),
  submitButton: document.querySelector("#chatForm button[type='submit']"),
};

const roles = Array.isArray(topRolesData.roles) ? topRolesData.roles : [];
const params = new URLSearchParams(window.location.search);
const requestedRoleName = params.get("role") || "";
const role = roles.find((item) => item.name === requestedRoleName) || roles[0] || null;

if (elements.backToIndexLink) {
  elements.backToIndexLink.href = new URL("./index.html", import.meta.url).href;
}

function updateSummaryProgress() {
  const summary = elements.roleSummary;
  const thumb = elements.summaryProgressThumb;
  if (!summary || !thumb) {
    return;
  }

  const scrollable = summary.scrollHeight - summary.clientHeight;
  if (scrollable <= 0) {
    thumb.style.opacity = "0";
    thumb.style.height = "100%";
    thumb.style.transform = "translateY(0)";
    return;
  }

  const visibleRatio = Math.max(summary.clientHeight / summary.scrollHeight, 0.18);
  const thumbHeightPx = Math.max(summary.clientHeight * visibleRatio, 28);
  const maxTravel = summary.clientHeight - thumbHeightPx;
  const progress = summary.scrollTop / scrollable;

  thumb.style.opacity = "1";
  thumb.style.height = `${thumbHeightPx}px`;
  thumb.style.transform = `translateY(${maxTravel * progress}px)`;
}

function appendMessage(text, type) {
  if (!elements.messages) {
    return;
  }

  const item = document.createElement("article");
  item.className = `chat-message chat-message-${type}`;
  item.textContent = text;
  elements.messages.appendChild(item);
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function setPending(pending) {
  state.pending = pending;
  if (elements.input) {
    elements.input.disabled = pending;
  }
  if (elements.submitButton) {
    elements.submitButton.disabled = pending;
    elements.submitButton.textContent = pending ? "生成中..." : "发送";
  }
}

function rolePayload() {
  if (!role) {
    return {};
  }

  return {
    name: role.name,
    title: role.title || role.name,
    aliases: role.aliases || [],
    summary: role.summary || role.description || "",
    personality: role.personality || [],
    events: (role.events || []).slice(0, 12).map((event) => ({
      title: event.title || "",
      event_id: event.event_id || "",
      status: event.status || "",
      decision: event.decision || "",
      result: event.result || "",
    })),
  };
}

function fallbackReply(input) {
  if (!role) {
    return "当前没有可用角色数据。";
  }

  const normalizedInput = input.trim().toLowerCase();
  const matchedEvent = (role.events || []).find((event) => {
    const bucket = [event.title, event.status, event.decision, event.result]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return normalizedInput && bucket.includes(normalizedInput);
  });

  if (matchedEvent) {
    const title = matchedEvent.title || matchedEvent.event_id || "该事件";
    const decision = matchedEvent.decision || "暂无记载";
    return `${role.name}：关于“${title}”，我记得当时的抉择是“${decision}”。`;
  }

  if (normalizedInput.includes("性格") || normalizedInput.includes("特点")) {
    const traits = (role.personality || []).slice(0, 4).join("、");
    return `${role.name}：若只说性情，我常被概括为 ${traits || "暂无标签"}。`;
  }

  return `${role.name}：我听见了。你可以继续问我经历、性格，或点右侧任一事件开始聊。`;
}

async function requestModelReply(message) {
  const response = await fetch("/api/role-dialogue", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      message,
      role: rolePayload(),
      history: state.history.slice(-12),
    }),
  });

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }

  const reply = String(data.reply || "").trim();
  if (!reply) {
    throw new Error("empty reply");
  }
  return reply;
}

function renderRole() {
  if (!role) {
    elements.roleName.textContent = "未找到角色";
    elements.roleSummary.textContent = "角色数据不存在，无法进入对话。";
    updateSummaryProgress();
    return;
  }

  document.title = `${role.name} · 角色对话`;
  elements.roleName.textContent = role.name;
  elements.roleAliases.textContent = role.aliases?.length
    ? `别名：${role.aliases.join(" / ")}`
    : "别名：暂无";
  elements.roleSummary.textContent = role.summary || role.description || "暂无角色摘要。";
  elements.roleSummary.scrollTop = 0;

  elements.traits.innerHTML = "";
  (role.personality?.length ? role.personality : ["暂无标签"]).forEach((trait) => {
    const chip = document.createElement("span");
    chip.className = "trait-chip";
    chip.textContent = trait;
    elements.traits.appendChild(chip);
  });

  elements.hints.innerHTML = "";
  (role.events || []).slice(0, 5).forEach((event) => {
    const hint = document.createElement("button");
    hint.type = "button";
    hint.className = "dialogue-hint";
    hint.textContent = event.title || event.event_id;
    hint.addEventListener("click", () => {
      elements.input.value = `聊聊${event.title || event.event_id}`;
      elements.input.focus();
    });
    elements.hints.appendChild(hint);
  });

  const greeting = `${role.name}：我已在此。你可以直接向我提问。`;
  appendMessage(greeting, "role");
  state.history.push({ role: "assistant", content: greeting });
  updateSummaryProgress();
}

elements.form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.pending) {
    return;
  }

  const value = elements.input.value.trim();
  if (!value) {
    return;
  }

  appendMessage(value, "user");
  state.history.push({ role: "user", content: value });
  elements.input.value = "";
  setPending(true);

  try {
    const reply = await requestModelReply(value);
    appendMessage(reply, "role");
    state.history.push({ role: "assistant", content: reply });
  } catch {
    const reply = fallbackReply(value);
    appendMessage(reply, "role");
    state.history.push({ role: "assistant", content: reply });
  } finally {
    setPending(false);
  }
});

elements.roleSummary?.addEventListener("scroll", updateSummaryProgress);
window.addEventListener("resize", updateSummaryProgress);

renderRole();

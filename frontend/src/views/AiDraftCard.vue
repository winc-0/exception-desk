<script setup lang="ts">
import { ref } from "vue";
import { api, ApiError, streamEvents } from "../api";

const props = defineProps<{ ticketId: string }>();
const emit = defineEmits<{ finished: [] }>();

const generating = ref(false);
const validated = ref(false);
const draft = ref("");
const original = ref("");
const draftId = ref("");
const source = ref<"model" | "template" | null>(null);
const reason = ref("");
const missing = ref<string[]>([]);
const risks = ref<string[]>([]);
const progress = ref("");
const error = ref("");
const notice = ref("");

const TOOL_LABELS: Record<string, string> = {
  get_ticket: "工单信息",
  get_evidence_status: "证据情况",
  get_claim: "索赔情况",
};

async function generate() {
  generating.value = true;
  validated.value = false;
  draft.value = "";
  draftId.value = "";
  source.value = null;
  error.value = "";
  notice.value = "";
  progress.value = "准备中…";
  try {
    await streamEvents(`/api/tickets/${props.ticketId}/ai/reply-draft`, (event, data) => {
      if (event === "facts") {
        missing.value = data.missing_required as string[];
        risks.value = data.risks as string[];
      } else if (event === "tool") {
        progress.value = `正在读取${(data.names as string[]).map((n) => TOOL_LABELS[n] ?? n).join("、")}…`;
      } else if (event === "delta") {
        progress.value = "正在起草，校验通过前不要使用…";
        draft.value += data.text as string;
      } else if (event === "reset") {
        progress.value = "内容没有通过校验，正在重写…";
        draft.value = "";
      } else if (event === "done") {
        // 以校验后的结果为准，流式显示的只是过程
        draft.value = data.draft as string;
        original.value = data.draft as string;
        draftId.value = data.draft_id as string;
        source.value = data.source as "model" | "template";
        reason.value = data.reason as string;
        validated.value = true;
      }
    });
    if (!validated.value) error.value = "连接中断了，请重新生成";
    emit("finished");
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "生成失败，请稍后再试";
  } finally {
    generating.value = false;
    progress.value = "";
  }
}

async function copy() {
  error.value = "";
  try {
    await navigator.clipboard.writeText(draft.value);
  } catch {
    error.value = "复制失败，请手动选中文字复制";
    return;
  }
  notice.value = "已复制，发送前请再核对一遍";
  void feedback(draft.value === original.value ? "used" : "edited");
}

async function feedback(verdict: "used" | "edited" | "rejected") {
  // 只用于统计，失败不打扰客服
  await api(`/api/tickets/${props.ticketId}/ai/reply-draft/${draftId.value}/feedback`, {
    method: "POST",
    body: { verdict },
  }).catch(() => {});
}

function discard() {
  void feedback("rejected");
  draft.value = "";
  draftId.value = "";
  source.value = null;
  notice.value = "";
}
</script>

<template>
  <div class="card ai-draft">
    <div class="card-head">
      <h3>客户回复草稿</h3>
      <button class="secondary" type="button" :disabled="generating" @click="generate">
        {{ generating ? "生成中…" : draft ? "重新生成" : "生成草稿" }}
      </button>
    </div>

    <ul v-if="risks.length" class="risks">
      <li v-for="risk in risks" :key="risk">{{ risk }}</li>
    </ul>

    <p v-if="progress" class="muted" role="status">{{ progress }}</p>

    <template v-if="draft || generating">
      <label>
        回复内容（可以修改）
        <textarea v-model="draft" rows="5" :readonly="!validated" maxlength="2000"></textarea>
      </label>
      <p v-if="source === 'template'" class="muted">
        这是模板内容，没有经过模型。{{ reason === "未配置模型" ? "" : "原因：模型结果未通过校验或调用失败。" }}
      </p>
      <div v-if="validated && draft" class="actions">
        <button type="button" @click="copy">复制</button>
        <button class="ghost" type="button" @click="discard">不用这份</button>
        <span class="muted">系统不会自动发送，请自己核对后发给客户</span>
      </div>
    </template>
    <p v-else class="muted">根据工单和证据情况起草一条给客户的回复，由你确认后再发。</p>

    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, useTemplateRef } from "vue";
import { api, ApiError, type Attachment, type EvidenceCategory, type TicketDetail } from "../api";
import { formatSize, formatTime } from "../format";

const props = defineProps<{ ticket: TicketDetail }>();
const emit = defineEmits<{ updated: [ticket: TicketDetail]; stale: [] }>();

const THUMBNAIL_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

const category = ref<EvidenceCategory | "">("");
const uploading = ref("");
const errors = ref<string[]>([]);
const fileInput = useTemplateRef<HTMLInputElement>("fileInput");

const voiding = ref<string | null>(null);
const voidReason = ref("");
const busy = ref(false);

const LEVEL_ORDER = { required: 0, recommended: 1, optional: 2 };
const LEVEL_LABELS = { required: "必备", recommended: "建议", optional: "可选" };

const checklist = computed(() =>
  // 可选项只有传了文件才显示，清单保持简短
  props.ticket.evidence_checklist
    .filter((item) => item.level !== "optional" || item.count > 0)
    .sort((a, b) => LEVEL_ORDER[a.level] - LEVEL_ORDER[b.level]),
);
const categoryOptions = computed(() =>
  [...props.ticket.evidence_checklist].sort((a, b) => LEVEL_ORDER[a.level] - LEVEL_ORDER[b.level]),
);
const labels = computed(
  () => Object.fromEntries(props.ticket.evidence_checklist.map((i) => [i.category, i.label])) as Record<string, string>,
);

function contentUrl(attachment: Attachment) {
  return `/api/tickets/${props.ticket.id}/attachments/${attachment.id}/content`;
}

async function upload(event: Event) {
  const files = [...((event.target as HTMLInputElement).files ?? [])];
  if (!files.length || !category.value) return;
  errors.value = [];
  // 手机上常一次选好几张，逐个上传。一个失败不影响其他文件
  for (const [index, file] of files.entries()) {
    uploading.value = files.length > 1 ? `上传中 ${index + 1}/${files.length}…` : "上传中…";
    try {
      const params = new URLSearchParams({ category: category.value, filename: file.name });
      emit(
        "updated",
        await api<TicketDetail>(`/api/tickets/${props.ticket.id}/attachments?${params}`, {
          method: "POST",
          file,
        }),
      );
    } catch (e) {
      errors.value.push(`${file.name}：${e instanceof ApiError ? e.message : "上传失败"}`);
    }
  }
  uploading.value = "";
  // 清空选择，否则再选同一个文件不会触发 change
  if (fileInput.value) fileInput.value.value = "";
}

function startVoid(attachment: Attachment) {
  voiding.value = attachment.id;
  voidReason.value = "";
  errors.value = [];
}

async function confirmVoid() {
  busy.value = true;
  errors.value = [];
  try {
    emit(
      "updated",
      await api<TicketDetail>(`/api/tickets/${props.ticket.id}/attachments/${voiding.value}/void`, {
        method: "POST",
        body: { reason: voidReason.value },
      }),
    );
    voiding.value = null;
  } catch (e) {
    errors.value = [e instanceof ApiError ? e.message : "操作失败，请稍后再试"];
    if (e instanceof ApiError && e.status === 409) emit("stale");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="card evidence">
    <h3>证据</h3>

    <ul v-if="checklist.length" class="checklist">
      <li v-for="item in checklist" :key="item.category" :class="{ missing: item.level === 'required' && !item.count }">
        <span aria-hidden="true">{{ item.count ? "✓" : "○" }}</span>
        {{ item.label }}
        <span class="muted">{{ LEVEL_LABELS[item.level] }}{{ item.count ? ` · ${item.count} 份` : "" }}</span>
      </li>
    </ul>

    <ul v-if="ticket.attachments.length" class="attachments">
      <li v-for="attachment in ticket.attachments" :key="attachment.id" :class="{ voided: attachment.voided_at }">
        <a class="thumb" :href="contentUrl(attachment)" target="_blank" rel="noopener">
          <img
            v-if="THUMBNAIL_TYPES.has(attachment.content_type) && !attachment.voided_at"
            :src="contentUrl(attachment)"
            :alt="attachment.filename"
            loading="lazy"
          />
          <span v-else>{{ attachment.content_type.split("/")[1].toUpperCase() }}</span>
        </a>
        <div class="attachment-info">
          <a :href="contentUrl(attachment)" target="_blank" rel="noopener" :title="`SHA-256 ${attachment.sha256}`">
            {{ attachment.filename }}
          </a>
          <div class="muted">
            {{ labels[attachment.category] }} · {{ formatSize(attachment.size_bytes) }} ·
            {{ attachment.uploader_name }} · {{ formatTime(attachment.created_at) }}
          </div>
          <div v-if="attachment.voided_at" class="muted">已作废：{{ attachment.void_reason }}</div>
          <form v-if="voiding === attachment.id" class="pending" @submit.prevent="confirmVoid">
            <label>
              作废原因
              <input v-model="voidReason" required maxlength="500" />
            </label>
            <div class="actions">
              <button type="submit" :disabled="busy || !voidReason.trim()">确认作废</button>
              <button class="ghost" type="button" @click="voiding = null">放弃</button>
            </div>
          </form>
        </div>
        <button
          v-if="attachment.can_void && voiding !== attachment.id"
          class="ghost"
          type="button"
          @click="startVoid(attachment)"
        >
          作废
        </button>
      </li>
    </ul>
    <p v-else class="muted">还没有证据。</p>

    <div v-if="errors.length" class="error" role="alert">
      <div v-for="message in errors" :key="message">{{ message }}</div>
    </div>

    <div v-if="ticket.can_upload" class="upload">
      <select v-model="category" aria-label="证据类别">
        <option value="" disabled>选择证据类别</option>
        <option v-for="item in categoryOptions" :key="item.category" :value="item.category">
          {{ item.label }}
        </option>
      </select>
      <input
        ref="fileInput"
        class="visually-hidden"
        type="file"
        multiple
        accept="image/*,video/*,.pdf,.heic"
        aria-label="选择证据文件"
        @change="upload"
      />
      <button class="secondary" type="button" :disabled="!category || !!uploading" @click="fileInput?.click()">
        {{ uploading || "上传文件" }}
      </button>
      <span class="muted">支持图片、PDF、MP4、MOV，单个不超过 50 MB。不要上传身份证和银行卡</span>
    </div>
  </div>
</template>

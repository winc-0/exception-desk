<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  api,
  ApiError,
  STATUS_LABELS,
  TYPE_LABELS,
  type Member,
  type TicketDetail,
  type TicketEvent,
  type TicketStatus,
} from "../api";
import { formatTime } from "../format";
import { can, me } from "../session";

const props = defineProps<{ id: string }>();

const ticket = ref<TicketDetail | null>(null);
const members = ref<Member[]>([]);
const loading = ref(true);
const notFound = ref(false);
const error = ref("");
const busy = ref(false);
const comment = ref("");

const pending = ref<TicketStatus | null>(null);
const pendingText = ref("");

// 按钮文字取决于“从哪到哪”，比如 处理中 和 重新打开 目标状态相同
function actionLabel(from: TicketStatus, to: TicketStatus): string {
  if (from === "open" && to === "in_progress") return "开始处理";
  if (from === "waiting" && to === "in_progress") return "继续处理";
  if (from === "resolved" && to === "in_progress") return "重新打开";
  return { waiting: "等待外部", resolved: "标记解决", closed: "关闭工单", cancelled: "取消工单" }[
    to as "waiting" | "resolved" | "closed" | "cancelled"
  ];
}

function needsText(from: TicketStatus, to: TicketStatus): boolean {
  return to === "waiting" || to === "resolved" || to === "cancelled" || (from === "resolved" && to === "in_progress");
}

const pendingPrompt = computed(() => (pending.value === "resolved" ? "处理结果" : "原因"));

const activeMembers = computed(() => members.value.filter((m) => m.is_active));

function describe(event: TicketEvent): string {
  switch (event.kind) {
    case "created":
      return event.data.source === "csv" ? "通过 CSV 导入创建了工单" : "创建了工单";
    case "assigned":
      if (!event.data.assignee_name) return "取消了指派";
      return event.data.assignee_name === event.actor_name ? "领取了工单" : `指派给 ${event.data.assignee_name}`;
    case "status_changed":
      return `把状态从“${STATUS_LABELS[event.from_status!]}”改为“${STATUS_LABELS[event.to_status!]}”`;
    case "commented":
      return "发表了评论";
  }
}

async function load() {
  try {
    ticket.value = await api<TicketDetail>(`/api/tickets/${props.id}`);
    notFound.value = false;
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 422)) notFound.value = true;
    else error.value = e instanceof ApiError ? e.message : "加载失败";
  } finally {
    loading.value = false;
  }
}

// 所有修改操作共用：成功后用返回值刷新页面；版本冲突时重新加载，让用户看到别人刚做的修改
async function act(path: string, body: unknown): Promise<boolean> {
  error.value = "";
  busy.value = true;
  try {
    ticket.value = await api<TicketDetail>(`/api/tickets/${props.id}/${path}`, { method: "POST", body });
    return true;
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "操作失败，请稍后再试";
    if (e instanceof ApiError && e.status === 409) await load();
    return false;
  } finally {
    busy.value = false;
  }
}

function chooseTransition(to: TicketStatus) {
  if (!ticket.value) return;
  // 每次都清空，上一步填的原因不能带到下一步的审计记录里
  pendingText.value = "";
  if (needsText(ticket.value.status, to)) {
    pending.value = to;
  } else {
    pending.value = null;
    void transition(to);
  }
}

async function transition(to: TicketStatus) {
  if (!ticket.value) return;
  const text = pendingText.value.trim();
  const ok = await act("transition", {
    to,
    version: ticket.value.version,
    ...(to === "resolved" ? { resolution: text } : { note: text }),
  });
  if (ok) {
    pending.value = null;
    pendingText.value = "";
  }
}

function assign(assigneeId: string | null) {
  if (ticket.value) void act("assign", { assignee_id: assigneeId, version: ticket.value.version });
}

async function submitComment() {
  if (await act("comments", { body: comment.value })) comment.value = "";
}

onMounted(async () => {
  await load();
  if (can("members:read")) {
    members.value = await api<Member[]>("/api/members").catch(() => []);
  }
});

watch(
  () => props.id,
  () => {
    loading.value = true;
    void load();
  },
);
</script>

<template>
  <p><RouterLink class="back" to="/tickets">← 返回工单列表</RouterLink></p>
  <p v-if="loading" class="muted">加载中…</p>
  <p v-else-if="notFound" class="empty">工单不存在，或者你没有查看权限。</p>
  <p v-else-if="!ticket" class="error" role="alert">
    {{ error }} <button class="ghost" type="button" @click="load">重试</button>
  </p>

  <div v-else class="detail">
    <section class="detail-main">
      <div class="card">
        <div class="ticket-head">
          <span class="mono muted">#{{ ticket.number }}</span>
          <span class="tag" :class="ticket.type">{{ TYPE_LABELS[ticket.type] }}</span>
          <span class="status" :class="ticket.status">{{ STATUS_LABELS[ticket.status] }}</span>
        </div>
        <h2>{{ ticket.title }}</h2>
        <p v-if="ticket.description" class="pre-line">{{ ticket.description }}</p>
        <div v-if="ticket.resolution" class="resolution">
          <strong>处理结果</strong>
          <p class="pre-line">{{ ticket.resolution }}</p>
        </div>
      </div>

      <p v-if="error" class="error" role="alert">{{ error }}</p>

      <div class="card">
        <h3>处理记录</h3>
        <ol class="timeline">
          <li v-for="event in ticket.events" :key="event.id">
            <div>
              <strong>{{ event.actor_name }}</strong> {{ describe(event) }}
              <span class="muted nowrap">{{ formatTime(event.created_at) }}</span>
            </div>
            <p v-if="event.body" class="event-body pre-line">{{ event.body }}</p>
          </li>
        </ol>
        <form v-if="ticket.can_comment" class="comment" @submit.prevent="submitComment">
          <label>
            添加评论
            <textarea v-model="comment" rows="2" required maxlength="2000"></textarea>
          </label>
          <div>
            <button class="secondary" type="submit" :disabled="busy || !comment.trim()">发表评论</button>
          </div>
        </form>
      </div>
    </section>

    <aside class="detail-side">
      <div class="card">
        <dl>
          <dt>运单号</dt>
          <dd class="mono">{{ ticket.tracking_number ?? "—" }}</dd>
          <dt>创建人</dt>
          <dd>{{ ticket.creator_name }}</dd>
          <dt>创建时间</dt>
          <dd>{{ formatTime(ticket.created_at) }}</dd>
          <dt>更新时间</dt>
          <dd>{{ formatTime(ticket.updated_at) }}</dd>
          <dt>处理人</dt>
          <dd>
            <select
              v-if="ticket.can_assign"
              :value="ticket.assignee_id ?? ''"
              :disabled="busy"
              aria-label="指派处理人"
              @change="assign(($event.target as HTMLSelectElement).value || null)"
            >
              <option value="" :disabled="ticket.status === 'in_progress' || ticket.status === 'waiting'">未指派</option>
              <option v-for="member in activeMembers" :key="member.user_id" :value="member.user_id">
                {{ member.display_name }}
              </option>
              <!-- 处理人已被停用时也要能显示 -->
              <option
                v-if="ticket.assignee_id && !activeMembers.some((m) => m.user_id === ticket?.assignee_id)"
                :value="ticket.assignee_id"
                disabled
              >
                {{ ticket.assignee_name }}
              </option>
            </select>
            <template v-else>{{ ticket.assignee_name ?? "未指派" }}</template>
          </dd>
        </dl>
      </div>

      <div v-if="ticket.can_claim || ticket.allowed_transitions.length" class="card action-card">
        <h3>操作</h3>
        <button v-if="ticket.can_claim" type="button" :disabled="busy" @click="assign(me!.user_id)">领取工单</button>
        <button
          v-for="to in ticket.allowed_transitions"
          :key="to"
          :class="to === 'cancelled' ? 'danger' : 'secondary'"
          type="button"
          :disabled="busy"
          @click="chooseTransition(to)"
        >
          {{ actionLabel(ticket.status, to) }}
        </button>

        <form v-if="pending" class="pending" @submit.prevent="transition(pending!)">
          <label>
            {{ actionLabel(ticket.status, pending) }}：{{ pendingPrompt }}
            <textarea v-model="pendingText" rows="3" required maxlength="2000"></textarea>
          </label>
          <div class="actions">
            <button type="submit" :disabled="busy || !pendingText.trim()">确认</button>
            <button class="ghost" type="button" @click="pending = null">放弃</button>
          </div>
        </form>
      </div>
    </aside>
  </div>
</template>

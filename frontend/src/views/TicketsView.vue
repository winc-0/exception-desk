<script setup lang="ts">
import { onMounted, reactive, ref, useTemplateRef } from "vue";
import { useRouter } from "vue-router";
import {
  api,
  ApiError,
  STATUS_LABELS,
  TYPE_LABELS,
  type ImportResult,
  type RowError,
  type Ticket,
  type TicketDetail,
  type TicketPage,
  type TicketType,
} from "../api";
import { formatTime } from "../format";
import { can } from "../session";

const router = useRouter();
const tickets = ref<Ticket[]>([]);
const nextBefore = ref<number | null>(null);
const loading = ref(true);
const loadingMore = ref(false);
const loadError = ref("");
const filters = reactive({ status: "", type: "", assignee: "", q: "" });

const showForm = ref(false);
const submitting = ref(false);
const formError = ref("");
const blank = () => ({ type: "damaged" as TicketType, title: "", tracking_number: "", description: "" });
const form = reactive(blank());

const importing = ref(false);
const importNotice = ref("");
const importError = ref("");
const importRowErrors = ref<RowError[]>([]);
const fileInput = useTemplateRef<HTMLInputElement>("fileInput");

function message(e: unknown, fallback: string) {
  return e instanceof ApiError ? e.message : fallback;
}

function queryString(before?: number) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value.trim()) params.set(key, value.trim());
  }
  if (before) params.set("before", String(before));
  const query = params.toString();
  return query ? `?${query}` : "";
}

let latestRequest = 0;

async function load() {
  // 连续切换筛选时只采用最后一次请求的结果
  const request = ++latestRequest;
  loading.value = true;
  loadError.value = "";
  try {
    const page = await api<TicketPage>(`/api/tickets${queryString()}`);
    if (request !== latestRequest) return;
    tickets.value = page.items;
    nextBefore.value = page.next_before;
  } catch (e) {
    if (request === latestRequest) loadError.value = message(e, "加载失败");
  } finally {
    if (request === latestRequest) loading.value = false;
  }
}

async function loadMore() {
  if (nextBefore.value === null) return;
  const request = latestRequest;
  loadingMore.value = true;
  try {
    const page = await api<TicketPage>(`/api/tickets${queryString(nextBefore.value)}`);
    if (request !== latestRequest) return;
    tickets.value.push(...page.items);
    nextBefore.value = page.next_before;
  } catch (e) {
    loadError.value = message(e, "加载失败");
  } finally {
    loadingMore.value = false;
  }
}

async function submit() {
  formError.value = "";
  submitting.value = true;
  try {
    const created = await api<TicketDetail>("/api/tickets", {
      method: "POST",
      body: { ...form, tracking_number: form.tracking_number || null },
    });
    await router.push(`/tickets/${created.id}`);
  } catch (e) {
    formError.value = message(e, "创建失败");
  } finally {
    submitting.value = false;
  }
}

async function importFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  importing.value = true;
  importNotice.value = "";
  importError.value = "";
  importRowErrors.value = [];
  try {
    const result = await api<ImportResult>("/api/tickets/import", { method: "POST", file });
    importNotice.value =
      `已导入 ${result.created} 张工单` +
      (result.skipped.length
        ? `，跳过 ${result.skipped.length} 行（第 ${result.skipped.map((s) => s.line).join("、")} 行已有未关闭的同类工单）`
        : "");
    await load();
  } catch (e) {
    importError.value = message(e, "导入失败");
    importRowErrors.value = e instanceof ApiError ? e.rowErrors : [];
  } finally {
    importing.value = false;
    // 清空选择，否则改好文件后再选同名文件不会触发 change
    if (fileInput.value) fileInput.value.value = "";
  }
}

onMounted(load);
</script>

<template>
  <div class="heading">
    <h2>工单</h2>
    <div class="actions">
      <template v-if="can('tickets:import')">
        <input
          ref="fileInput"
          class="visually-hidden"
          type="file"
          accept=".csv,text/csv"
          aria-label="选择要导入的 CSV 文件"
          @change="importFile"
        />
        <button class="secondary" type="button" :disabled="importing" @click="fileInput?.click()">
          {{ importing ? "导入中…" : "导入 CSV" }}
        </button>
      </template>
      <button type="button" @click="showForm = !showForm">{{ showForm ? "收起" : "新建工单" }}</button>
    </div>
  </div>

  <p v-if="can('tickets:import')" class="hint">
    CSV 第一行写列名：类型、标题、运单号、描述。类型填破损、丢件、超时、改址或其他，类型和标题必填。
  </p>
  <p v-if="importNotice" class="notice" role="status">{{ importNotice }}</p>
  <div v-if="importError" class="error" role="alert">
    {{ importError }}
    <ul v-if="importRowErrors.length">
      <li v-for="row in importRowErrors.slice(0, 20)" :key="row.line">第 {{ row.line }} 行：{{ row.message }}</li>
      <li v-if="importRowErrors.length > 20">还有 {{ importRowErrors.length - 20 }} 行有问题</li>
    </ul>
  </div>

  <form v-if="showForm" class="card form-grid" @submit.prevent="submit">
    <label>
      异常类型
      <select v-model="form.type">
        <option v-for="(label, value) in TYPE_LABELS" :key="value" :value="value">{{ label }}</option>
      </select>
    </label>
    <label>
      运单号（选填）
      <input v-model.trim="form.tracking_number" maxlength="64" />
    </label>
    <label class="wide">
      标题
      <input v-model.trim="form.title" required maxlength="200" />
    </label>
    <label class="wide">
      描述（选填）
      <textarea v-model="form.description" rows="3" maxlength="5000"></textarea>
    </label>
    <p v-if="formError" class="error wide" role="alert">{{ formError }}</p>
    <div class="wide">
      <button type="submit" :disabled="submitting">{{ submitting ? "创建中…" : "创建" }}</button>
    </div>
  </form>

  <form class="filters" role="search" @submit.prevent="load">
    <select v-model="filters.status" aria-label="按状态筛选" @change="load">
      <option value="">全部状态</option>
      <option v-for="(label, value) in STATUS_LABELS" :key="value" :value="value">{{ label }}</option>
    </select>
    <select v-model="filters.type" aria-label="按类型筛选" @change="load">
      <option value="">全部类型</option>
      <option v-for="(label, value) in TYPE_LABELS" :key="value" :value="value">{{ label }}</option>
    </select>
    <select v-model="filters.assignee" aria-label="按处理人筛选" @change="load">
      <option value="">全部处理人</option>
      <option value="me">我处理的</option>
      <option value="unassigned">未指派</option>
    </select>
    <input v-model="filters.q" type="search" maxlength="100" placeholder="搜索标题或运单号" aria-label="搜索标题或运单号" />
    <button class="secondary" type="submit">搜索</button>
  </form>

  <p v-if="loadError" class="error" role="alert">
    {{ loadError }} <button class="ghost" type="button" @click="load">重试</button>
  </p>
  <p v-if="loading" class="muted">加载中…</p>
  <p v-else-if="tickets.length === 0 && !loadError" class="empty">没有符合条件的工单。</p>
  <template v-else-if="tickets.length">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>工单号</th>
            <th>类型</th>
            <th>状态</th>
            <th>标题</th>
            <th>运单号</th>
            <th>处理人</th>
            <th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="ticket in tickets" :key="ticket.id">
            <td class="mono">#{{ ticket.number }}</td>
            <td><span class="tag" :class="ticket.type">{{ TYPE_LABELS[ticket.type] }}</span></td>
            <td class="nowrap"><span class="status" :class="ticket.status">{{ STATUS_LABELS[ticket.status] }}</span></td>
            <td>
              <RouterLink class="title-link" :to="`/tickets/${ticket.id}`">{{ ticket.title }}</RouterLink>
            </td>
            <td class="mono">{{ ticket.tracking_number ?? "—" }}</td>
            <td class="nowrap">{{ ticket.assignee_name ?? "未指派" }}</td>
            <td class="nowrap">{{ formatTime(ticket.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="nextBefore !== null" class="load-more">
      <button class="secondary" type="button" :disabled="loadingMore" @click="loadMore">
        {{ loadingMore ? "加载中…" : "加载更多" }}
      </button>
    </div>
  </template>
</template>

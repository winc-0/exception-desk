<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { api, ApiError, TYPE_LABELS, type Ticket, type TicketType } from "../api";

const tickets = ref<Ticket[]>([]);
const loading = ref(true);
const loadError = ref("");
const formError = ref("");
const submitting = ref(false);
const showForm = ref(false);
const blank = () => ({ type: "damaged" as TicketType, title: "", tracking_number: "", description: "" });
const form = reactive(blank());

const timeFormat = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    tickets.value = await api<Ticket[]>("/api/tickets");
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : "加载失败";
  } finally {
    loading.value = false;
  }
}

async function submit() {
  formError.value = "";
  submitting.value = true;
  try {
    const created = await api<Ticket>("/api/tickets", {
      method: "POST",
      body: { ...form, tracking_number: form.tracking_number || null },
    });
    tickets.value.unshift(created);
    Object.assign(form, blank());
    showForm.value = false;
  } catch (e) {
    formError.value = e instanceof ApiError ? e.message : "创建失败";
  } finally {
    submitting.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="heading">
    <h2>工单</h2>
    <button type="button" @click="showForm = !showForm">{{ showForm ? "收起" : "新建工单" }}</button>
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

  <p v-if="loadError" class="error" role="alert">
    {{ loadError }} <button class="ghost" type="button" @click="load">重试</button>
  </p>
  <p v-else-if="loading" class="muted">加载中…</p>
  <p v-else-if="tickets.length === 0" class="empty">还没有工单，点右上角新建第一张。</p>
  <div v-else class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>类型</th>
          <th>标题</th>
          <th>运单号</th>
          <th>创建人</th>
          <th>处理人</th>
          <th>创建时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="ticket in tickets" :key="ticket.id">
          <td><span class="tag" :class="ticket.type">{{ TYPE_LABELS[ticket.type] }}</span></td>
          <td>{{ ticket.title }}</td>
          <td class="mono">{{ ticket.tracking_number ?? "—" }}</td>
          <td>{{ ticket.creator_name }}</td>
          <td>{{ ticket.assignee_name ?? "未指派" }}</td>
          <td class="nowrap">{{ timeFormat.format(new Date(ticket.created_at)) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

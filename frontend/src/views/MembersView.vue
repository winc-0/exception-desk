<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { api, ApiError, ROLE_LABELS, type Member, type Role } from "../api";
import { can, me } from "../session";

const members = ref<Member[]>([]);
const error = ref("");
const notice = ref("");
const showForm = ref(false);
const submitting = ref(false);
const blank = () => ({ email: "", display_name: "", role: "agent" as Role, password: "" });
const form = reactive(blank());

function message(e: unknown) {
  return e instanceof ApiError ? e.message : "操作失败，请稍后再试";
}

async function load() {
  try {
    members.value = await api<Member[]>("/api/members");
  } catch (e) {
    error.value = message(e);
  }
}

async function add() {
  error.value = "";
  notice.value = "";
  submitting.value = true;
  try {
    const created = await api<Member & { password_set: boolean }>("/api/members", {
      method: "POST",
      body: form,
    });
    members.value.push(created);
    notice.value = created.password_set
      ? `已添加 ${created.display_name}，请把初始密码安全地告诉对方。`
      : `${created.email} 已有账号，已加入本公司，对方继续使用原密码登录。`;
    Object.assign(form, blank());
    showForm.value = false;
  } catch (e) {
    error.value = message(e);
  } finally {
    form.password = "";
    submitting.value = false;
  }
}

async function update(member: Member, patch: { role?: Role; is_active?: boolean }) {
  error.value = "";
  notice.value = "";
  try {
    Object.assign(member, await api<Member>(`/api/members/${member.user_id}`, { method: "PATCH", body: patch }));
    if (member.user_id === me.value?.user_id && patch.role) {
      me.value = await api("/api/auth/me");
    }
  } catch (e) {
    error.value = message(e);
    await load();
  }
}

onMounted(load);
</script>

<template>
  <div class="heading">
    <h2>成员</h2>
    <button v-if="can('members:manage')" type="button" @click="showForm = !showForm">
      {{ showForm ? "收起" : "添加成员" }}
    </button>
  </div>

  <form v-if="showForm" class="card form-grid" @submit.prevent="add">
    <label>
      姓名
      <input v-model.trim="form.display_name" required maxlength="64" />
    </label>
    <label>
      邮箱
      <input v-model.trim="form.email" type="email" required autocomplete="off" />
    </label>
    <label>
      角色
      <select v-model="form.role">
        <option v-for="(label, value) in ROLE_LABELS" :key="value" :value="value">{{ label }}</option>
      </select>
    </label>
    <label>
      初始密码（至少 10 位）
      <input v-model="form.password" type="password" required minlength="10" maxlength="128" autocomplete="new-password" />
    </label>
    <div class="wide">
      <button type="submit" :disabled="submitting">{{ submitting ? "添加中…" : "添加" }}</button>
    </div>
  </form>

  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <p v-if="notice" class="notice" role="status">{{ notice }}</p>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>姓名</th>
          <th>邮箱</th>
          <th>角色</th>
          <th>状态</th>
          <th v-if="can('members:manage')">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="member in members" :key="member.user_id" :class="{ inactive: !member.is_active }">
          <td>{{ member.display_name }}</td>
          <td>{{ member.email }}</td>
          <td>
            <select
              v-if="can('members:manage')"
              :value="member.role"
              :aria-label="`${member.display_name} 的角色`"
              @change="update(member, { role: ($event.target as HTMLSelectElement).value as Role })"
            >
              <option v-for="(label, value) in ROLE_LABELS" :key="value" :value="value">{{ label }}</option>
            </select>
            <template v-else>{{ ROLE_LABELS[member.role] }}</template>
          </td>
          <td>{{ member.is_active ? "启用" : "已停用" }}</td>
          <td v-if="can('members:manage')">
            <button class="ghost" type="button" @click="update(member, { is_active: !member.is_active })">
              {{ member.is_active ? "停用" : "启用" }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

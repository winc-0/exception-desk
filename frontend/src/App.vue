<script setup lang="ts">
import { useRouter } from "vue-router";
import { api, ROLE_LABELS } from "./api";
import { can, me } from "./session";

const router = useRouter();

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
    me.value = null;
    await router.push("/login");
  }
}
</script>

<template>
  <header v-if="me" class="topbar">
    <div class="brand">
      <strong>异常件协同台</strong>
      <span class="muted">{{ me.tenant.name }}</span>
    </div>
    <nav>
      <RouterLink to="/tickets">工单</RouterLink>
      <RouterLink v-if="can('claims:manage')" to="/claims">索赔</RouterLink>
      <RouterLink v-if="can('members:read')" to="/members">成员</RouterLink>
    </nav>
    <div class="account">
      <span>{{ me.display_name }} · {{ ROLE_LABELS[me.role] }}</span>
      <button class="ghost" type="button" @click="logout">退出</button>
    </div>
  </header>
  <main :class="{ page: me }">
    <RouterView />
  </main>
</template>

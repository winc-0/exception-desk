<script setup lang="ts">
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { api, ApiError, type Me } from "../api";
import { me } from "../session";

const router = useRouter();
const form = reactive({ tenant_slug: "", email: "", password: "" });
const error = ref("");
const submitting = ref(false);

async function submit() {
  error.value = "";
  submitting.value = true;
  try {
    me.value = await api<Me>("/api/auth/login", { method: "POST", body: form });
    await router.push("/tickets");
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "登录失败，请稍后再试";
  } finally {
    form.password = "";
    submitting.value = false;
  }
}
</script>

<template>
  <div class="login">
    <form class="card" @submit.prevent="submit">
      <h1>异常件协同台</h1>
      <p class="muted">破损、丢件、超时、改址，一处跟进到底。</p>
      <label>
        公司代码
        <input v-model.trim="form.tenant_slug" required autocomplete="organization" />
      </label>
      <label>
        邮箱
        <input v-model.trim="form.email" type="email" required autocomplete="username" />
      </label>
      <label>
        密码
        <input v-model="form.password" type="password" required autocomplete="current-password" />
      </label>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button type="submit" :disabled="submitting">{{ submitting ? "登录中…" : "登录" }}</button>
    </form>
  </div>
</template>

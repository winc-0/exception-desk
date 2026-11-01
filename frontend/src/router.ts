import { createRouter, createWebHistory } from "vue-router";
import { can, me, restoreSession } from "./session";
import LoginView from "./views/LoginView.vue";
import MembersView from "./views/MembersView.vue";
import TicketsView from "./views/TicketsView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", component: LoginView, meta: { public: true } },
    { path: "/", redirect: "/tickets" },
    { path: "/tickets", component: TicketsView },
    { path: "/members", component: MembersView, meta: { permission: "members:read" } },
    { path: "/:pathMatch(.*)*", redirect: "/tickets" },
  ],
});

router.beforeEach(async (to) => {
  await restoreSession().catch(() => {});
  if (to.meta.public) return me.value ? "/tickets" : true;
  if (!me.value) return "/login";
  const permission = to.meta.permission as string | undefined;
  if (permission && !can(permission)) return "/tickets";
  return true;
});

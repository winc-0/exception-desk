import { createApp } from "vue";
import { setUnauthorizedHandler } from "./api";
import App from "./App.vue";
import { router } from "./router";
import { me } from "./session";
import "./style.css";

setUnauthorizedHandler(() => {
  me.value = null;
  void router.push("/login");
});

createApp(App).use(router).mount("#app");

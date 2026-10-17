import WebKit

/// 登录页自动填充。
///
/// 上游登录框已经写了 autocomplete="username" / "current-password"，密码本身交给系统
/// 密码库；但界面每次冷启动都停在空白的登录页上，连上次的用户名都不认得。这里补两件小事：
/// 提交时把用户名（按开关连密码一起）记在本地存储里，下次进登录页填回去并把光标放在
/// 用户名上，让系统的自动填充建议一次点击就能出来。
enum LoginAutofill {
    private static let script = """
    (() => {
      const USER = "MoviePilotApp.lastUser";
      const PASS = "MoviePilotApp.lastPassword";
      const FLAG = "MoviePilotApp.rememberPassword";

      const wantsPassword = () => localStorage.getItem(FLAG) !== "0";

      function fields() {
        const user = document.querySelector('input[autocomplete="username"]');
        if (!user) return null;
        const form = user.closest("form") || document;
        const pass = form.querySelector('input[autocomplete="current-password"]');
        return pass ? { user, pass } : null;
      }

      function write(input, value) {
        // 输入框由 Vue 托管，直接改 value 组件看不见，得补发一次 input。
        // 取描述符走元素自己的原型链，不依赖 HTMLInputElement 这类全局构造器。
        let proto = Object.getPrototypeOf(input), desc = null;
        while (proto && !desc) {
          desc = Object.getOwnPropertyDescriptor(proto, "value");
          proto = Object.getPrototypeOf(proto);
        }
        if (desc && desc.set) { desc.set.call(input, value); } else { input.value = value; }
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }

      function apply() {
        const found = fields();
        if (!found) return;
        const user = localStorage.getItem(USER);
        const pass = localStorage.getItem(PASS);
        let touched = false;
        if (user && !found.user.value) { write(found.user, user); touched = true; }
        if (pass && wantsPassword() && !found.pass.value) { write(found.pass, pass); touched = true; }
        if (touched && !found.user.value.length) return;
        if (touched) found.user.focus();
      }

      function capture() {
        const found = fields();
        if (!found) return;
        if (found.user.value) localStorage.setItem(USER, found.user.value);
        if (found.pass.value && wantsPassword()) localStorage.setItem(PASS, found.pass.value);
      }

      document.addEventListener("submit", capture, true);
      document.addEventListener("click", (event) => {
        const button = event.target && event.target.closest ? event.target.closest("button") : null;
        if (button && button.type === "submit") capture();
      }, true);

      let queued = false;
      const schedule = () => {
        if (queued) return;
        queued = true;
        setTimeout(() => { queued = false; apply(); }, 120);
      };
      new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
      window.__mpLoginAutofill = true;
      apply();
    })();
    """

    static func userScript() -> WKUserScript {
        WKUserScript(source: script, injectionTime: .atDocumentEnd, forMainFrameOnly: true)
    }
}

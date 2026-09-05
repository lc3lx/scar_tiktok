async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(msg) {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2200);
}

function settingsPayload() {
  return {
    target_video_url: document.getElementById("videoUrl").value,
    profile_url: document.getElementById("profileUrl").value,
    bot_mode: document.getElementById("botMode").value,
    enable_liking: document.getElementById("likeEnabled").checked,
    enable_sharing: document.getElementById("shareEnabled").checked,
    enable_commenting: document.getElementById("commentEnabled").checked,
    auto_otp: document.getElementById("autoOtp").checked,
    browser_headless: document.getElementById("headless").checked,
    watch_count: Number(document.getElementById("watchCount").value || 0),
    max_browsers: Number(document.getElementById("maxBrowsers").value || 1),
    otp_timeout: Number(document.getElementById("otpTimeout").value || 90),
    imap_host: document.getElementById("imapHost").value || "imap.hostinger.com",
    imap_port: Number(document.getElementById("imapPort").value || 993),
  };
}

function fillComments(c) {
  const pending = (c && c.pending) || [];
  document.getElementById("comments").value = pending.join("\n");
  document.getElementById("commentsHint").textContent =
    `متبقي: ${c?.pending_count ?? pending.length} | مستخدم: ${c?.used_count ?? 0} — كل حساب يأخذ تعليقاً ويُحذف`;
}

function fillSettings(s, comments) {
  document.getElementById("videoUrl").value = s.target_video_url || "";
  document.getElementById("profileUrl").value = s.profile_url || "";
  document.getElementById("botMode").value = s.bot_mode || "watch";
  document.getElementById("likeEnabled").checked = !!s.enable_liking;
  document.getElementById("shareEnabled").checked = s.enable_sharing !== false;
  document.getElementById("commentEnabled").checked = !!s.enable_commenting;
  document.getElementById("autoOtp").checked = s.auto_otp !== false;
  document.getElementById("headless").checked = s.browser_headless !== false;
  document.getElementById("watchCount").value = s.watch_count ?? 0;
  document.getElementById("maxBrowsers").value = s.max_browsers || 1;
  document.getElementById("otpTimeout").value = s.otp_timeout || 90;
  document.getElementById("imapHost").value = s.imap_host || "imap.hostinger.com";
  document.getElementById("imapPort").value = s.imap_port || 993;
  if (comments) fillComments(comments);
  else fillComments({ pending: s.comment_texts || [], pending_count: (s.comment_texts || []).length, used_count: 0 });
}

function fillMailboxSelect(mailboxes, selected) {
  const sel = document.getElementById("accMailbox");
  const current = selected || sel.value;
  sel.innerHTML = "";
  if (!mailboxes.length) {
    sel.innerHTML = '<option value="">أضف صندوق Hostinger أولاً</option>';
    return;
  }
  mailboxes.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.email;
    opt.textContent = (m.label ? m.label + " — " : "") + m.email;
    sel.appendChild(opt);
  });
  if (current && [...sel.options].some((o) => o.value === current)) {
    sel.value = current;
  }
}

function renderMailboxes(mailboxes) {
  const list = document.getElementById("mailboxList");
  fillMailboxSelect(mailboxes);
  if (!mailboxes.length) {
    list.innerHTML = '<li style="justify-content:center;color:var(--muted)">لا يوجد صناديق Hostinger</li>';
    return;
  }
  list.innerHTML = mailboxes
    .map(
      (m) => `
    <li>
      <div>
        <div>${escapeHtml(m.email)}</div>
        <div class="meta">${escapeHtml(m.label || "صندوق ميل")}</div>
      </div>
      <button class="btn danger" data-mb="${escapeAttr(m.email)}">حذف</button>
    </li>`
    )
    .join("");

  list.querySelectorAll("button[data-mb]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("حذف صندوق Hostinger؟")) return;
      try {
        const data = await api("/api/mailboxes", {
          method: "DELETE",
          body: JSON.stringify({ email: btn.dataset.mb }),
        });
        renderMailboxes(
          (data.mailboxes || []).map((m) => ({
            email: m.email,
            label: m.label,
            has_password: !!m.password,
          }))
        );
        toast("تم حذف الصندوق");
      } catch (e) {
        toast("فشل الحذف: " + e.message);
      }
    });
  });
}

function renderAccounts(accounts) {
  const list = document.getElementById("accountList");
  if (!accounts.length) {
    list.innerHTML = '<li style="justify-content:center;color:var(--muted)">لا توجد حسابات</li>';
    return;
  }
  list.innerHTML = accounts
    .map(
      (a) => `
    <li>
      <div>
        <div>${escapeHtml(a.email)}</div>
        <div class="meta">OTP عبر: ${escapeHtml(a.mailbox || "غير محدد")}</div>
      </div>
      <button class="btn danger" data-email="${escapeAttr(a.email)}">حذف</button>
    </li>`
    )
    .join("");

  list.querySelectorAll("button[data-email]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("حذف الحساب؟")) return;
      try {
        const data = await api("/api/accounts", {
          method: "DELETE",
          body: JSON.stringify({ email: btn.dataset.email }),
        });
        renderAccounts(data.accounts);
        toast("تم الحذف");
      } catch (e) {
        toast("فشل الحذف: " + e.message);
      }
    });
  });
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
function escapeAttr(s) {
  return String(s).replace(/"/g, "&quot;");
}

function setRunning(running, error, stopping) {
  const dot = document.getElementById("statusDot");
  const text = document.getElementById("statusText");
  const startBtn = document.getElementById("startBot");
  const stopBtn = document.getElementById("stopBot");
  if (stopping) {
    dot.className = "dot running";
    text.textContent = "جاري الإيقاف...";
  } else {
    dot.className = "dot" + (running ? " running" : error ? " error" : "");
    text.textContent = running ? "يعمل الآن..." : error ? "توقف بخطأ" : "جاهز";
  }
  startBtn.style.display = running ? "none" : "inline-block";
  stopBtn.style.display = running ? "inline-block" : "none";
  stopBtn.disabled = !!stopping;
  document.getElementById("runHint").textContent = stopping
    ? "يتم إيقاف البوت وإغلاق المتصفحات..."
    : running
    ? "البوت يعمل — يمكنك الإيقاف في أي وقت"
    : "عدّل الإعدادات من الأعلى ثم شغّل البوت";
}

async function refresh() {
  try {
    const data = await api("/api/status");
    fillSettings(data.settings || {}, data.comments);
    renderMailboxes(data.mailboxes || []);
    renderAccounts(data.accounts || []);
    setRunning(data.running, data.error, data.stopping);
    if (data.logs) document.getElementById("logs").textContent = data.logs;
  } catch (e) {
    console.error(e);
  }
}

document.getElementById("appendComments").addEventListener("click", async () => {
  try {
    const data = await api("/api/comments", {
      method: "POST",
      body: JSON.stringify({
        comments: document.getElementById("comments").value,
        replace: false,
      }),
    });
    fillComments(data);
    toast(`تمت الإضافة — متبقي ${data.pending_count}`);
  } catch (e) {
    toast("فشل: " + e.message);
  }
});

document.getElementById("replaceComments").addEventListener("click", async () => {
  try {
    const data = await api("/api/comments", {
      method: "POST",
      body: JSON.stringify({
        comments: document.getElementById("comments").value,
        replace: true,
      }),
    });
    fillComments(data);
    toast(`تم الاستبدال — متبقي ${data.pending_count}`);
  } catch (e) {
    toast("فشل: " + e.message);
  }
});

document.getElementById("saveSettings").addEventListener("click", async () => {
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify(settingsPayload()),
    });
    toast("تم حفظ الإعدادات");
  } catch (e) {
    toast("فشل الحفظ: " + e.message);
  }
});

document.getElementById("addMailbox").addEventListener("click", async () => {
  try {
    const data = await api("/api/mailboxes", {
      method: "POST",
      body: JSON.stringify({
        email: document.getElementById("mbEmail").value,
        password: document.getElementById("mbPass").value,
        label: document.getElementById("mbLabel").value,
      }),
    });
    document.getElementById("mbEmail").value = "";
    document.getElementById("mbPass").value = "";
    document.getElementById("mbLabel").value = "";
    renderMailboxes(
      (data.mailboxes || []).map((m) => ({
        email: m.email,
        label: m.label,
        has_password: !!m.password,
      }))
    );
    toast("تم حفظ صندوق Hostinger");
  } catch (e) {
    toast("فشل الحفظ: " + e.message);
  }
});

document.getElementById("addAccount").addEventListener("click", async () => {
  try {
    const mailbox = document.getElementById("accMailbox").value;
    if (!mailbox) {
      toast("اختر صندوق Hostinger أولاً");
      return;
    }
    const data = await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify({
        email: document.getElementById("accEmail").value,
        password: document.getElementById("accPass").value,
        mailbox,
      }),
    });
    document.getElementById("accEmail").value = "";
    document.getElementById("accPass").value = "";
    renderAccounts(data.accounts);
    toast("تمت إضافة الحساب");
  } catch (e) {
    toast("فشل الإضافة: " + e.message);
  }
});

async function importAccounts(replace) {
  try {
    const mailbox = document.getElementById("accMailbox").value;
    if (!mailbox) {
      toast("اختر صندوق Hostinger أولاً");
      return;
    }
    const data = await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify({
        bulk: document.getElementById("bulkAccounts").value,
        mailbox,
        replace: !!replace,
      }),
    });
    renderAccounts(data.accounts);
    toast(replace ? "تم استبدال الحسابات" : "تمت إضافة المجموعة");
  } catch (e) {
    toast("فشل الاستيراد: " + e.message);
  }
}

document.getElementById("importBulk").addEventListener("click", () => importAccounts(false));
document.getElementById("replaceBulk").addEventListener("click", () => {
  if (confirm("استبدال كل الحسابات الحالية؟")) importAccounts(true);
});

document.getElementById("refreshAccounts").addEventListener("click", refresh);

document.getElementById("startBot").addEventListener("click", async () => {
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify(settingsPayload()),
    });
    await api("/api/start", { method: "POST", body: "{}" });
    setRunning(true, null, false);
    toast("بدأ التشغيل");
  } catch (e) {
    toast("فشل التشغيل: " + e.message);
  }
});

document.getElementById("stopBot").addEventListener("click", async () => {
  try {
    await api("/api/stop", { method: "POST", body: "{}" });
    setRunning(true, null, true);
    toast("تم إرسال أمر الإيقاف...");
  } catch (e) {
    toast("فشل الإيقاف: " + e.message);
  }
});

refresh();
setInterval(async () => {
  try {
    const data = await api("/api/status");
    setRunning(data.running, data.error, data.stopping);
    renderMailboxes(data.mailboxes || []);
    renderAccounts(data.accounts || []);
    if (data.comments) fillComments(data.comments);
    if (data.logs) document.getElementById("logs").textContent = data.logs;
  } catch (_) {}
}, 2500);

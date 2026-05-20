// app/static/chat/chat.js

(function () {
  "use strict";

  function initGroupSearch(inputId, listId) {
    const input = document.getElementById(inputId);
    const list = document.getElementById(listId);
    if (!input || !list) return;

    const items = list.querySelectorAll(".chat-group-item[data-group-name]");
    input.addEventListener("input", function () {
      const keyword = (input.value || "").trim().toLowerCase();
      items.forEach(function (item) {
        const name = (item.getAttribute("data-group-name") || "").toLowerCase();
        item.style.display = !keyword || name.indexOf(keyword) !== -1 ? "" : "none";
      });
    });
  }

  function initCollapsible(toggleId, contentId) {
    const toggle = document.getElementById(toggleId);
    const content = document.getElementById(contentId);
    if (!toggle || !content) return;

    toggle.addEventListener("click", function () {
      content.classList.toggle("is-open");
    });
  }

  function initGroupDrawer() {
    const toggleBtn = document.getElementById("chatGroupDrawerToggle");
    const closeBtn = document.getElementById("chatGroupDrawerClose");
    const drawer = document.getElementById("chatGroupDrawer");
    const backdrop = document.getElementById("chatDrawerBackdrop");
    if (!toggleBtn || !drawer || !backdrop) return;

    function openDrawer() {
      drawer.hidden = false;
      backdrop.hidden = false;
    }

    function closeDrawer() {
      drawer.hidden = true;
      backdrop.hidden = true;
    }

    toggleBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      openDrawer();
    });

    if (closeBtn) {
      closeBtn.addEventListener("click", closeDrawer);
    }

    backdrop.addEventListener("click", closeDrawer);
    drawer.addEventListener("click", function (event) {
      event.stopPropagation();
    });


    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !drawer.hidden) {
        closeDrawer();
      }
    });
  }

  function initMemberPanel() {
    const toggleBtn = document.getElementById("memberPanelToggle");
    const closeBtn = document.getElementById("memberPanelClose");
    const panel = document.getElementById("chatMembersPanel");
    if (!toggleBtn || !panel) return;

    function closePanel() {
      panel.hidden = true;
    }

    toggleBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      panel.hidden = !panel.hidden;
    });

    if (closeBtn) {
      closeBtn.addEventListener("click", closePanel);
    }

    panel.addEventListener("click", function (event) {
      event.stopPropagation();
    });

    document.addEventListener("click", function () {
      if (!panel.hidden) {
        closePanel();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !panel.hidden) {
        closePanel();
      }
    });
  }

  function initConfirmForms() {
    const forms = document.querySelectorAll("form[data-confirm]");
    forms.forEach(function (form) {
      form.addEventListener("submit", function (event) {
        const message = form.getAttribute("data-confirm") || "Xác nhận thực hiện thao tác này?";
        if (!window.confirm(message)) {
          event.preventDefault();
        }
      });
    });
  }

  function initScrollMessagesToBottom() {
    const box = document.getElementById("chatMessages");
    if (box) {
      box.scrollTop = box.scrollHeight;
    }
  }

  function dispatchSystemNotify(payload) {
    try {
      window.dispatchEvent(
        new CustomEvent("hvgl:notify", {
          detail: payload || {}
        })
      );
    } catch (err) {
      // bỏ qua
    }
  }
  
  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function truncateText(value, maxLength) {
    const text = String(value || "").trim();
    if (!text) return "";
    if (text.length <= maxLength) return text;
    return text.slice(0, maxLength - 1) + "…";
  }

  function clearReplyState() {
    const replyIdInput = document.getElementById("chatReplyToMessageId");
    const replyingBar = document.getElementById("chatReplyingBar");
    const replyingText = document.getElementById("chatReplyingText");

    if (replyIdInput) {
      replyIdInput.value = "";
    }
    if (replyingText) {
      replyingText.textContent = "";
    }
    if (replyingBar) {
      replyingBar.hidden = true;
    }
  }

  function buildPinnedItemHtml(pin) {
    if (!pin) return "";
    const messageId = escapeHtml(pin.message_id || "");
    const attachmentId = escapeHtml(pin.attachment_id || "");
    const label = escapeHtml(pin.label || (pin.pin_kind === "attachment" ? "File" : "Tin nhắn"));
    const title = escapeHtml(pin.title || "Tin đã ghim");
    const meta = escapeHtml([
      pin.sender_name || "Người dùng",
      pin.pinned_by_name || "",
      pin.pinned_at_text || ""
    ].filter(Boolean).join(" · "));

    return [
      '<button type="button" class="chat-pinned-item" data-pin-kind="' + escapeHtml(pin.pin_kind || "message") + '" data-message-id="' + messageId + '"' + (attachmentId ? ' data-attachment-id="' + attachmentId + '"' : "") + '>',
      '<span class="chat-pinned-item-label">' + label + '</span>',
      '<span class="chat-pinned-item-title">' + title + '</span>',
      '<span class="chat-pinned-item-meta">' + meta + '</span>',
      '</button>'
    ].join("");
  }

  function refreshPinnedPanelState() {
    const panel = document.getElementById("chatPinnedPanel");
    const list = document.getElementById("chatPinnedList");
    const count = document.getElementById("chatPinnedCount");
    if (!panel || !list || !count) return;

    const items = list.querySelectorAll(".chat-pinned-item");
    const empty = document.getElementById("chatPinnedEmpty");
    if (!items.length) {
      panel.classList.add("is-empty");
      if (!empty) {
        list.innerHTML = '<div class="chat-pinned-empty" id="chatPinnedEmpty">Chưa có tin nhắn hoặc file nào được ghim.</div>';
      }
    } else {
      panel.classList.remove("is-empty");
      if (empty) empty.remove();
    }
    count.textContent = String(items.length) + " mục";
  }

  function upsertPinnedItem(pin) {
    if (!pin || !pin.is_pinned) return;
    const list = document.getElementById("chatPinnedList");
    if (!list) return;

    const selector = pin.pin_kind === "attachment"
      ? '.chat-pinned-item[data-attachment-id="' + CSS.escape(pin.attachment_id || "") + '"]'
      : '.chat-pinned-item[data-message-id="' + CSS.escape(pin.message_id || "") + '"][data-pin-kind="message"]';

    const existing = list.querySelector(selector);
    const html = buildPinnedItemHtml(pin);
    if (existing) {
      existing.outerHTML = html;
    } else {
      list.insertAdjacentHTML("afterbegin", html);
    }
    refreshPinnedPanelState();
  }

  function removePinnedItem(pinKind, messageId, attachmentId) {
    const list = document.getElementById("chatPinnedList");
    if (!list) return;

    let selector = '';
    if (pinKind === "attachment" && attachmentId) {
      selector = '.chat-pinned-item[data-attachment-id="' + CSS.escape(attachmentId) + '"]';
    } else if (messageId) {
      selector = '.chat-pinned-item[data-message-id="' + CSS.escape(messageId) + '"][data-pin-kind="message"]';
    }

    if (!selector) return;
    const item = list.querySelector(selector);
    if (item) item.remove();
    refreshPinnedPanelState();
  }

  function scrollToPinnedTarget(messageId, attachmentId) {
    if (!messageId) return;
    const row = document.querySelector('.chat-message-row[data-message-id="' + CSS.escape(messageId) + '"]');
    if (!row) return;

    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.add("is-highlighted");
    window.setTimeout(function () {
      row.classList.remove("is-highlighted");
    }, 1800);

    if (attachmentId) {
      const att = row.querySelector('.chat-attachment-item[data-attachment-id="' + CSS.escape(attachmentId) + '"]');
      if (att) {
        att.classList.add("is-highlighted");
        window.setTimeout(function () {
          att.classList.remove("is-highlighted");
        }, 1800);
      }
    }
  }
  
  function buildAttachmentsHtml(message) {
    const attachments = message.attachments || [];
    if (!attachments.length || message.recalled) return "";

    return [
      '<div class="chat-attachments">',
      attachments
        .filter(function (att) {
          return !att.deleted_by_owner;
        })
        .map(function (att) {
          const attachmentId = escapeHtml(att.id || "");
          const messageId = escapeHtml(message.id || "");
          const filename = escapeHtml(att.filename || "Tệp đính kèm");
          const rawPreviewUrl = att.preview_url || att.path || "#";
          const rawDownloadUrl = att.download_url || att.path || "#";
          const previewUrl = escapeHtml(rawPreviewUrl);
          const downloadUrl = escapeHtml(rawDownloadUrl);
          const isRecalled = !!att.recalled;
          const canManage = !!message.is_mine && !isRecalled;
          const isPinned = !!att.is_pinned;

          return [
            '<div class="chat-attachment-item" data-attachment-id="' + attachmentId + '" data-message-id="' + messageId + '" data-is-pinned="' + (isPinned ? "1" : "0") + '">',
            '<div class="chat-attachment-links">',
            (
              isRecalled
                ? '<span class="chat-attachment-link">📎 ' + filename + "</span>"
                : '<a href="' + previewUrl + '" target="_blank" class="chat-attachment-link">📎 ' + filename + "</a>"
            ),
            (
              isRecalled
                ? ""
                : '<a href="' + previewUrl + '" target="_blank" class="chat-attachment-mini-link">Xem</a>'
            ),
            (
              isRecalled
                ? ""
                : '<a href="' + downloadUrl + '" class="chat-attachment-mini-link">Tải về</a>'
            ),
            (
              canManage
                ? '<button type="button" class="chat-attachment-mini-link js-toggle-pin-attachment" data-attachment-id="' + attachmentId + '" data-message-id="' + messageId + '" data-filename="' + filename + '" data-is-pinned="' + (isPinned ? "1" : "0") + '">' + (isPinned ? "Bỏ ghim file" : "Ghim file") + "</button>"
                : ""
            ),
            (
              canManage
                ? '<button type="button" class="chat-attachment-mini-link js-recall-attachment" data-attachment-id="' + attachmentId + '" data-message-id="' + messageId + '" data-filename="' + filename + '">Thu hồi file</button>'
                : ""
            ),
            (
              !!message.is_mine
                ? '<button type="button" class="chat-attachment-mini-link js-delete-attachment" data-attachment-id="' + attachmentId + '" data-message-id="' + messageId + '" data-filename="' + filename + '">Xóa file</button>'
                : ""
            ),
            "</div>",
            "</div>"
          ].join("");
        }).join(""),
      "</div>"
    ].join("");
  }

  function buildReplyHtml(message) {
    if (!message.reply_preview) return "";
    const replyContent = String(message.reply_preview.content || "");
    const replyText = replyContent.indexOf("/static/sticker/") === 0 ? "[Sticker]" : replyContent;
    return [
      '<div class="chat-reply-preview">',
      '<div class="chat-reply-preview-name">' + escapeHtml(message.reply_preview.sender_name || "Người dùng") + "</div>",
      '<div class="chat-reply-preview-text">' + escapeHtml(replyText) + "</div>",
      "</div>"
    ].join("");
  }

  function isStickerMessage(message) {
    return String((message && message.message_type) || "TEXT").toUpperCase() === "STICKER";
  }

  function isSafeStickerUrl(value) {
    const url = String(value || "").trim();
    return url.indexOf("/static/sticker/") === 0 && !url.includes("..") && !url.includes("\\");
  }

  function buildStickerHtml(stickerUrl) {
    const safeUrl = isSafeStickerUrl(stickerUrl) ? stickerUrl : "";
    if (!safeUrl) return "";
    return '<div class="chat-message-sticker"><img src="' + escapeHtml(safeUrl) + '" alt="Sticker"></div>';
  }

  function buildReactionBar(message) {
    const counts = message.reaction_counts || { like: 0, heart: 0, laugh: 0 };
    return [
      '<div class="chat-reaction-bar" data-message-id="' + escapeHtml(message.id || "") + '">',
      '<button type="button" class="chat-reaction-btn" data-reaction="like">👍 <span>' + (counts.like || 0) + "</span></button>",
      '<button type="button" class="chat-reaction-btn" data-reaction="heart">❤️ <span>' + (counts.heart || 0) + "</span></button>",
      '<button type="button" class="chat-reaction-btn" data-reaction="laugh">😄 <span>' + (counts.laugh || 0) + "</span></button>",
      "</div>"
    ].join("");
  }

  function buildMessageActions(message) {
    const copyText = escapeHtml(message.content || "");
    const canManage = !!message.is_mine && !message.recalled;

    return [
      '<div class="chat-message-actions">',
      '<button type="button" class="chat-mini-btn js-reply-message" data-message-id="' + escapeHtml(message.id || "") + '" data-reply-name="' + escapeHtml(message.sender_name || "Người dùng") + '" data-reply-text="' + copyText + '">Trả lời</button>',
      (!message.recalled ? '<button type="button" class="chat-mini-btn js-copy-message" data-copy-text="' + copyText + '">Chép</button>' : ""),
      '<button type="button" class="chat-mini-btn js-forward-message" data-message-id="' + escapeHtml(message.id || "") + '" data-message-type="' + escapeHtml(message.message_type || "TEXT") + '" data-forward-text="' + copyText + '">Chuyển tiếp</button>',
	  (!message.recalled ? '<button type="button" class="chat-mini-btn js-toggle-pin-message" data-message-id="' + escapeHtml(message.id || "") + '" data-is-pinned="' + (message.is_pinned ? '1' : '0') + '">' + (message.is_pinned ? 'Bỏ ghim' : 'Ghim') + '</button>' : ''),
      (canManage
        ? '<button type="button" class="chat-mini-btn chat-mini-btn-warning js-recall-message" data-message-id="' + escapeHtml(message.id || "") + '" data-message-type="' + escapeHtml(message.message_type || "TEXT") + '">Thu hồi tin nhắn</button>'
        : ""),
      (canManage
        ? '<button type="button" class="chat-mini-btn chat-mini-btn-danger js-delete-message" data-message-id="' + escapeHtml(message.id || "") + '" data-message-type="' + escapeHtml(message.message_type || "TEXT") + '">Xóa tin nhắn</button>'
        : ""),
      "</div>"
    ].join("");
  }

  function buildMessageHtml(message) {
    const rowClass = message.is_mine ? "chat-message-row is-mine" : "chat-message-row is-other";
    const bubbleClass = message.is_mine ? "chat-message-bubble is-mine" : "chat-message-bubble is-other";
    const timeClass = message.is_mine ? "chat-message-time is-mine" : "chat-message-time is-other";
    const toolsClass = message.is_mine ? "chat-message-tools is-mine" : "chat-message-tools is-other";

    const senderBlock = !message.is_mine
      ? '<div class="chat-message-sender">' + escapeHtml(message.sender_name || "Người dùng") + "</div>"
      : "";

    const contentHtml = message.recalled
      ? "<em>Tin nhắn đã được thu hồi.</em>"
      : (
          isStickerMessage(message)
            ? buildStickerHtml(message.content || "")
            : escapeHtml(message.content || "").replace(/\n/g, "<br>")
        );

    return [
      '<div class="' + rowClass + '" data-message-id="' + escapeHtml(message.id || "") + '" data-sender-name="' + escapeHtml(message.sender_name || "Người dùng") + '" data-created-at="' + escapeHtml(message.created_at_text || "") + '" data-message-type="' + escapeHtml(message.message_type || "TEXT") + '" data-is-pinned="' + (message.is_pinned ? '1' : '0') + '">',
      '<label class="chat-select-message"><input type="checkbox" class="chat-message-check" value="' + escapeHtml(message.id || "") + '"></label>',
      '<div class="chat-message-stack">',
      '<div class="' + bubbleClass + '">',
      senderBlock,
      buildReplyHtml(message),
      '<div class="chat-message-content">' + contentHtml + "</div>",
      buildAttachmentsHtml(message),
      "</div>",
      '<div class="' + toolsClass + '">',
      buildReactionBar(message),
      buildMessageActions(message),
      '<div class="' + timeClass + '">' + escapeHtml(message.created_at_text || "") + "</div>",
      "</div>",
      "</div>",
      "</div>"
    ].join("");
  }

  function appendMessageToRoom(message) {
    const box = document.getElementById("chatMessages");
    if (!box) return;

    const empty = document.getElementById("chatEmptyRoom");
    if (empty) {
      empty.remove();
    }

    box.insertAdjacentHTML("beforeend", buildMessageHtml(message));
    box.scrollTop = box.scrollHeight;
  }

  function updateReactionBar(messageId, counts) {
    const bar = document.querySelector('.chat-reaction-bar[data-message-id="' + CSS.escape(messageId) + '"]');
    if (!bar) return;

    ["like", "heart", "laugh"].forEach(function (rt) {
      const btn = bar.querySelector('[data-reaction="' + rt + '"] span');
      if (btn) {
        btn.textContent = String((counts && counts[rt]) || 0);
      }
    });
  }

  function applyMessageRecalled(messageId, content) {
    const row = document.querySelector('.chat-message-row[data-message-id="' + CSS.escape(messageId) + '"]');
    if (!row) return;

    const contentBox = row.querySelector(".chat-message-content");
    if (contentBox) {
      contentBox.innerHTML = "<em>" + escapeHtml(content || "Tin nhắn đã được thu hồi.") + "</em>";
    }

    row.querySelectorAll(".chat-attachments").forEach(function (node) {
      node.remove();
    });

    row.querySelectorAll(".js-copy-message, .js-recall-message, .js-delete-message").forEach(function (btn) {
      btn.remove();
    });
  }

  function removeMessageFromRoom(messageId) {
    const row = document.querySelector('.chat-message-row[data-message-id="' + CSS.escape(messageId) + '"]');
    if (row) {
      row.remove();
    }
    removePinnedItem("message", messageId, "");
  }

  function applyMessagePinState(messageId, isPinned, pinItem) {
    const row = document.querySelector('.chat-message-row[data-message-id="' + CSS.escape(messageId) + '"]');
    if (row) {
      row.setAttribute("data-is-pinned", isPinned ? "1" : "0");
      const btn = row.querySelector('.js-toggle-pin-message[data-message-id="' + CSS.escape(messageId) + '"]');
      if (btn) {
        btn.setAttribute("data-is-pinned", isPinned ? "1" : "0");
        btn.textContent = isPinned ? "Bỏ ghim" : "Ghim";
      }
    }

    if (isPinned && pinItem) {
      upsertPinnedItem({
        pin_kind: "message",
        is_pinned: true,
        message_id: messageId,
        attachment_id: "",
        label: pinItem.label,
        title: pinItem.title,
        sender_name: pinItem.sender_name,
        pinned_by_name: pinItem.pinned_by_name,
        pinned_at_text: pinItem.pinned_at_text
      });
    } else {
      removePinnedItem("message", messageId, "");
    }
  }

  function applyAttachmentPinState(attachmentId, messageId, isPinned, pinItem) {
    const row = document.querySelector('.chat-message-row[data-message-id="' + CSS.escape(messageId) + '"]');
    if (row) {
      const item = row.querySelector('.chat-attachment-item[data-attachment-id="' + CSS.escape(attachmentId) + '"]');
      if (item) {
        item.setAttribute("data-is-pinned", isPinned ? "1" : "0");
      }
      const btn = row.querySelector('.js-toggle-pin-attachment[data-attachment-id="' + CSS.escape(attachmentId) + '"]');
      if (btn) {
        btn.setAttribute("data-is-pinned", isPinned ? "1" : "0");
        btn.textContent = isPinned ? "Bỏ ghim file" : "Ghim file";
      }
    }

    if (isPinned && pinItem) {
      upsertPinnedItem({
        pin_kind: "attachment",
        is_pinned: true,
        message_id: messageId,
        attachment_id: attachmentId,
        label: pinItem.label,
        title: pinItem.title,
        sender_name: pinItem.sender_name,
        pinned_by_name: pinItem.pinned_by_name,
        pinned_at_text: pinItem.pinned_at_text
      });
    } else {
      removePinnedItem("attachment", messageId, attachmentId);
    }
  }
  
  function setGroupUnreadBadge(groupId, count) {
    if (!groupId) return;

    const badges = document.querySelectorAll('.chat-unread-badge[data-group-id="' + CSS.escape(groupId) + '"]');
    badges.forEach(function (badge) {
      const total = Number(count || 0);
      badge.textContent = String(total) + " mới";
      badge.classList.toggle("is-hidden", total <= 0);
    });
  }

  function setGroupNewBadge(groupId, isVisible) {
    if (!groupId) return;

    const badges = document.querySelectorAll('.chat-group-badge-new-group[data-group-id="' + CSS.escape(groupId) + '"]');
    badges.forEach(function (badge) {
      badge.classList.toggle("is-hidden", !isVisible);
    });
  }

  function hasGroupItemInDom(groupId) {
    if (!groupId) return false;

    if (document.querySelector('.chat-group-badge-new-group[data-group-id="' + CSS.escape(groupId) + '"]')) {
    return true;
    }
    if (document.querySelector('.chat-unread-badge[data-group-id="' + CSS.escape(groupId) + '"]')) {
      return true;
    }
    if (document.querySelector('.chat-group-item[href="/chat/' + CSS.escape(groupId) + '"]')) {
      return true;
    }
    return false;
  }

  function showNewGroupSoftRefreshNotice(payload) {
    const noticeId = "chatNewGroupSoftRefreshNotice";
    let notice = document.getElementById(noticeId);

    if (!notice) {
      notice = document.createElement("div");
      notice.id = noticeId;
      notice.setAttribute(
        "style",
        [
          "position:fixed",
          "right:18px",
          "bottom:18px",
          "z-index:4000",
          "max-width:360px",
          "padding:12px 14px",
          "border-radius:12px",
          "box-shadow:0 12px 30px rgba(15,23,42,0.22)",
          "background:#eff6ff",
          "border:1px solid #93c5fd",
          "color:#0f172a",
          "font-size:14px",
          "line-height:1.45"
        ].join(";")
      );
      notice.innerHTML = [
        '<div class="js-chat-new-group-text" style="font-weight:700;margin-bottom:10px;"></div>',
        '<div style="display:flex;gap:8px;justify-content:flex-end;">',
        '<button type="button" class="js-chat-new-group-later" style="border:none;border-radius:8px;padding:7px 10px;background:#e2e8f0;color:#0f172a;font-weight:700;cursor:pointer;">Để sau</button>',
        '<button type="button" class="js-chat-new-group-refresh" style="border:none;border-radius:8px;padding:7px 10px;background:#0b4f9c;color:#fff;font-weight:700;cursor:pointer;">Cập nhật danh sách nhóm</button>',
        "</div>"
      ].join("");
      document.body.appendChild(notice);
    }

    const text = notice.querySelector(".js-chat-new-group-text");
    const laterBtn = notice.querySelector(".js-chat-new-group-later");
    const refreshBtn = notice.querySelector(".js-chat-new-group-refresh");

    if (text) {
      text.textContent = "Anh vừa được thêm vào nhóm chat mới. Hệ thống không tự tải lại trang để tránh mất nội dung đang soạn.";
    }

    if (laterBtn && !laterBtn.dataset.boundClick) {
      laterBtn.dataset.boundClick = "1";
      laterBtn.addEventListener("click", function () {
        notice.hidden = true;
      });
    }

    if (refreshBtn && !refreshBtn.dataset.boundClick) {
      refreshBtn.dataset.boundClick = "1";
      refreshBtn.addEventListener("click", function () {
        window.location.reload();
      });
    }

    notice.hidden = false;
    dispatchSystemNotify(payload || {});
  }

  function getActiveGroupId() {
    const box = document.getElementById("chatMessages");
    if (!box) return "";
    return (box.getAttribute("data-group-id") || "").trim();
  }

  async function markActiveGroupReadRealtime() {
    const groupId = getActiveGroupId();
    if (!groupId) return;

    try {
      const response = await fetch("/chat/api/groups/" + encodeURIComponent(groupId) + "/read", {
        method: "POST",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          "Accept": "application/json"
        },
        credentials: "same-origin"
      });

      const data = await response.json().catch(function () { return null; });
      if (response.ok && data && data.ok) {
        setGroupUnreadBadge(groupId, 0);
        setGroupNewBadge(groupId, false);
      }
    } catch (err) {
      // bỏ qua để không làm gián đoạn chat
    }
  }

  function applyAttachmentRecalled(attachmentId, messageId, filename, content) {
    const row = document.querySelector('.chat-message-row[data-message-id="' + CSS.escape(messageId) + '"]');
    if (!row) return;

    const item = row.querySelector('.chat-attachment-item[data-attachment-id="' + CSS.escape(attachmentId) + '"]');
	removePinnedItem("attachment", messageId, attachmentId);
    if (item) {
      item.innerHTML = [
        '<div class="chat-attachment-links">',
        '<span class="chat-attachment-link">📎 ' + escapeHtml(filename || "[Đã thu hồi] Tệp đính kèm") + "</span>",
        '<button type="button" class="chat-attachment-mini-link js-delete-attachment" data-attachment-id="' + escapeHtml(attachmentId) + '" data-message-id="' + escapeHtml(messageId) + '" data-filename="' + escapeHtml(filename || "Tệp đính kèm") + '">Xóa file</button>',
        "</div>"
      ].join("");
    }

    if (content) {
      const contentBox = row.querySelector(".chat-message-content");
      if (contentBox) {
        contentBox.innerHTML = "<em>" + escapeHtml(content) + "</em>";
      }
    }
  }


  function applyAttachmentDeleted(attachmentId, messageId, content) {
    const row = document.querySelector('.chat-message-row[data-message-id="' + CSS.escape(messageId) + '"]');
    if (!row) return;

    const item = row.querySelector('.chat-attachment-item[data-attachment-id="' + CSS.escape(attachmentId) + '"]');
    removePinnedItem("attachment", messageId, attachmentId);

    if (item) {
      item.remove();
    }

    if (content) {
      const contentBox = row.querySelector(".chat-message-content");
      if (contentBox) {
        contentBox.innerHTML = "<em>" + escapeHtml(content) + "</em>";
      }
    }
  }

  function formatSingleCopyText(row) {
    if (!row) return "";

    const senderName = (row.getAttribute("data-sender-name") || "Người dùng").trim();
    const createdAt = (row.getAttribute("data-created-at") || "").trim();
    const messageType = (row.getAttribute("data-message-type") || "TEXT").trim().toUpperCase();
    const contentNode = row.querySelector(".chat-message-content");
    const contentText = contentNode ? contentNode.innerText.trim() : "";
    const attachments = Array.from(row.querySelectorAll(".chat-attachment-link")).map(function (el) {
      return (el.textContent || "").replace(/^📎\s*/, "").trim();
    }).filter(Boolean);

    const header = "[Người gửi gốc: " + senderName + (createdAt ? " | Thời gian gốc: " + createdAt : "") + "]";
    if (contentText === "Tin nhắn đã được thu hồi.") {
      return header + "\n[Tin nhắn đã được thu hồi]";
    }

    const lines = [header];

    if (messageType === "FILE" && attachments.length) {
      attachments.forEach(function (name) {
        lines.push("[Tên file: " + name + "]");
      });
    }

    if (contentText) {
      lines.push(contentText);
    }

    return lines.join("\n");
  }

  function formatMultiCopyText(rows) {
    const parts = rows.map(function (row) {
      return formatSingleCopyText(row);
    }).filter(Boolean);

    return parts.join("\n----------------\n");
  }

  function initReplyUi() {
    const box = document.getElementById("chatMessages");
    const replyIdInput = document.getElementById("chatReplyToMessageId");
    const replyingBar = document.getElementById("chatReplyingBar");
    const replyingText = document.getElementById("chatReplyingText");
    const cancelBtn = document.getElementById("chatReplyingCancel");
    const input = document.getElementById("chatComposerInput");

    if (!box || !replyIdInput || !replyingBar || !replyingText || !input) return;

    box.addEventListener("click", function (event) {
      const btn = event.target.closest(".js-reply-message");
      if (!btn) return;

      const messageId = btn.getAttribute("data-message-id") || "";
      const replyName = btn.getAttribute("data-reply-name") || "Người dùng";
      const replyText = btn.getAttribute("data-reply-text") || "";

      replyIdInput.value = messageId;
      replyingText.textContent = replyName + ": " + truncateText(replyText, 140);
      replyingBar.hidden = false;
      input.focus();
    });

    if (cancelBtn) {
      cancelBtn.addEventListener("click", function () {
        clearReplyState();
      });
    }
  }

  function initCopyForwardActions() {
    const box = document.getElementById("chatMessages");
    const modal = document.getElementById("chatShareModal");
    const backdrop = document.getElementById("chatShareBackdrop");
    const closeButton = document.getElementById("chatShareCloseButton");
    const cancelButton = document.getElementById("chatShareCancelButton");
    const form = document.getElementById("chatShareForm");
    const messageIdInput = document.getElementById("chatShareMessageId");
    const targetGroupInput = document.getElementById("chatShareTargetGroupId");
    const preview = document.getElementById("chatSharePreview");
    const note = document.getElementById("chatShareNote");
    const submitButton = document.getElementById("chatShareSubmitButton");

    if (!box) return;

    function setNote(message, isError) {
      if (!note) return;
      note.textContent = message || "";
      note.classList.toggle("is-error", !!isError);
    }

    function closeModal() {
      if (modal) modal.hidden = true;
      if (messageIdInput) messageIdInput.value = "";
      if (targetGroupInput) targetGroupInput.value = "";
      if (preview) preview.textContent = "";
      setNote("", false);
    }

    function openModal(messageId, previewText) {
      if (!modal || !messageIdInput || !targetGroupInput || !preview) return;
      messageIdInput.value = messageId || "";
      targetGroupInput.value = "";
      preview.textContent = truncateText(previewText || "", 220);
      setNote("", false);
      modal.hidden = false;
    }

    box.addEventListener("click", async function (event) {
      const copyBtn = event.target.closest(".js-copy-message");
      if (copyBtn) {
        const row = copyBtn.closest(".chat-message-row");
        const text = formatSingleCopyText(row);
        try {
          await navigator.clipboard.writeText(text);
        } catch (err) {
          window.prompt("Sao chép nội dung:", text);
        }
        return;
      }
	  
	  const recallAttachmentBtn = event.target.closest(".js-recall-attachment");
      if (recallAttachmentBtn) {
        const attachmentId = recallAttachmentBtn.getAttribute("data-attachment-id") || "";
        const messageId = recallAttachmentBtn.getAttribute("data-message-id") || "";
        const filename = recallAttachmentBtn.getAttribute("data-filename") || "Tệp đính kèm";
        if (!attachmentId) return;

        if (!window.confirm('Xác nhận thu hồi file "' + filename + '"?')) {
          return;
        }

        try {
          const response = await fetch("/chat/api/attachments/" + encodeURIComponent(attachmentId) + "/recall", {
            method: "POST",
            headers: {
              "X-Requested-With": "XMLHttpRequest",
              "Accept": "application/json"
            },
            credentials: "same-origin"
          });

          const data = await response.json().catch(function () { return null; });
          if (!response.ok || !data || !data.ok) {
            const detail = data && data.detail ? data.detail : "Không thu hồi được file.";
            throw new Error(detail);
          }

          applyAttachmentRecalled(
            data.attachment_id || attachmentId,
            data.message_id || messageId,
            data.filename || ("[Đã thu hồi] " + filename),
            data.content || ""
          );
        } catch (err) {
          window.alert(err && err.message ? err.message : "Không thu hồi được file.");
        }
        return;
      }

      const deleteAttachmentBtn = event.target.closest(".js-delete-attachment");
      if (deleteAttachmentBtn) {
        const attachmentId = deleteAttachmentBtn.getAttribute("data-attachment-id") || "";
        const messageId = deleteAttachmentBtn.getAttribute("data-message-id") || "";
        const filename = deleteAttachmentBtn.getAttribute("data-filename") || "Tệp đính kèm";
        if (!attachmentId) return;

        if (!window.confirm('Xác nhận xóa file "' + filename + '"?')) {
          return;
        }

        try {
          const response = await fetch("/chat/api/attachments/" + encodeURIComponent(attachmentId) + "/delete", {
            method: "POST",
            headers: {
              "X-Requested-With": "XMLHttpRequest",
              "Accept": "application/json"
            },
            credentials: "same-origin"
          });

          const data = await response.json().catch(function () { return null; });
          if (!response.ok || !data || !data.ok) {
            const detail = data && data.detail ? data.detail : "Không xóa được file.";
            throw new Error(detail);
          }

          applyAttachmentDeleted(
            data.attachment_id || attachmentId,
            data.message_id || messageId,
            data.content || ""
          );
        } catch (err) {
          window.alert(err && err.message ? err.message : "Không xóa được file.");
        }
        return;
      }

      const recallBtn = event.target.closest(".js-recall-message");
      if (recallBtn) {
        const messageId = recallBtn.getAttribute("data-message-id") || "";
        const messageType = (recallBtn.getAttribute("data-message-type") || "TEXT").toUpperCase();
        if (!messageId) return;

        const confirmText = messageType === "FILE"
          ? "Xác nhận thu hồi file đã gửi?"
          : "Xác nhận thu hồi tin nhắn này?";
        if (!window.confirm(confirmText)) return;

        try {
          const response = await fetch("/chat/api/messages/" + encodeURIComponent(messageId) + "/recall", {
            method: "POST",
            headers: {
              "X-Requested-With": "XMLHttpRequest",
              "Accept": "application/json"
            },
            credentials: "same-origin"
          });

          const data = await response.json().catch(function () { return null; });
          if (!response.ok || !data || !data.ok) {
            const detail = data && data.detail ? data.detail : "Không thu hồi được.";
            throw new Error(detail);
          }

          applyMessageRecalled(messageId, "Tin nhắn đã được thu hồi.");
        } catch (err) {
          window.alert(err && err.message ? err.message : "Không thu hồi được.");
        }
        return;
      }

      const deleteBtn = event.target.closest(".js-delete-message");
      if (deleteBtn) {
        const messageId = deleteBtn.getAttribute("data-message-id") || "";
        const messageType = (deleteBtn.getAttribute("data-message-type") || "TEXT").toUpperCase();
        if (!messageId) return;

        const confirmText = messageType === "FILE"
          ? "Xác nhận xóa file đã gửi? Thao tác này sẽ xóa hẳn khỏi cuộc trò chuyện."
          : "Xác nhận xóa tin nhắn này? Thao tác này sẽ xóa hẳn khỏi cuộc trò chuyện.";
        if (!window.confirm(confirmText)) return;

        try {
          const response = await fetch("/chat/api/messages/" + encodeURIComponent(messageId) + "/delete", {
            method: "POST",
            headers: {
              "X-Requested-With": "XMLHttpRequest",
              "Accept": "application/json"
            },
            credentials: "same-origin"
          });

          const data = await response.json().catch(function () { return null; });
          if (!response.ok || !data || !data.ok) {
            const detail = data && data.detail ? data.detail : "Không xóa được.";
            throw new Error(detail);
          }

          removeMessageFromRoom(messageId);
        } catch (err) {
          window.alert(err && err.message ? err.message : "Không xóa được.");
        }
        return;
      }

      const forwardBtn = event.target.closest(".js-forward-message, .js-share-message");
      if (forwardBtn) {
        const row = forwardBtn.closest(".chat-message-row");
        const previewText = formatSingleCopyText(row);
        openModal(forwardBtn.getAttribute("data-message-id") || "", previewText);
      }
    });

    if (backdrop) {
      backdrop.addEventListener("click", closeModal);
    }
    if (closeButton) {
      closeButton.addEventListener("click", closeModal);
    }
    if (cancelButton) {
      cancelButton.addEventListener("click", closeModal);
    }

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && modal && !modal.hidden) {
        closeModal();
      }
    });

    if (form) {
      form.addEventListener("submit", async function (event) {
        event.preventDefault();

        const messageId = messageIdInput ? (messageIdInput.value || "").trim() : "";
        const targetGroupId = targetGroupInput ? (targetGroupInput.value || "").trim() : "";

        if (!messageId) {
          setNote("Không xác định được tin nhắn cần chuyển tiếp.", true);
          return;
        }
        if (!targetGroupId) {
          setNote("Anh chưa chọn nhóm nhận.", true);
          return;
        }

        const fd = new FormData();
        fd.set("target_group_id", targetGroupId);

        if (submitButton) {
          submitButton.disabled = true;
          submitButton.classList.add("is-loading");
        }
        setNote("", false);

        try {
          const response = await fetch("/chat/api/messages/" + encodeURIComponent(messageId) + "/share", {
            method: "POST",
            body: fd,
            headers: {
              "X-Requested-With": "XMLHttpRequest",
              "Accept": "application/json"
            },
            credentials: "same-origin"
          });

          const data = await response.json().catch(function () { return null; });
          if (!response.ok || !data || !data.ok) {
            const detail = data && data.detail ? data.detail : "Không chuyển tiếp được tin nhắn.";
            throw new Error(detail);
          }

          closeModal();
          window.alert("Đã chuyển tiếp tin nhắn sang nhóm đã chọn.");
        } catch (err) {
          setNote(err && err.message ? err.message : "Không chuyển tiếp được tin nhắn.", true);
        } finally {
          if (submitButton) {
            submitButton.disabled = false;
            submitButton.classList.remove("is-loading");
          }
        }
      });
    }
  }

  function initBulkSelect() {
    const box = document.getElementById("chatMessages");
    const toolbar = document.getElementById("chatBulkToolbar");
    const countText = document.getElementById("chatBulkCount");
    const copyBtn = document.getElementById("chatCopySelectedBtn");
    const clearBtn = document.getElementById("chatClearSelectedBtn");
    if (!box || !toolbar || !countText || !copyBtn || !clearBtn) return;

    function selectedChecks() {
      return Array.from(box.querySelectorAll(".chat-message-check:checked"));
    }

    function refreshToolbar() {
      const selected = selectedChecks();
      toolbar.hidden = selected.length === 0;
      countText.textContent = "Đã chọn " + selected.length + " tin nhắn";
    }

    function clearSelected() {
      box.querySelectorAll(".chat-message-check").forEach(function (checkbox) {
        checkbox.checked = false;
      });
      refreshToolbar();
    }

    box.addEventListener("change", function (event) {
      if (event.target.classList.contains("chat-message-check")) {
        refreshToolbar();
      }
    });

    copyBtn.addEventListener("click", async function () {
      const selected = selectedChecks();
      const rows = selected.map(function (checkbox) {
        return checkbox.closest(".chat-message-row");
      }).filter(Boolean);

      const text = formatMultiCopyText(rows);
      if (!text) return;

      try {
        await navigator.clipboard.writeText(text);
      } catch (err) {
        window.prompt("Copy nhiều tin nhắn:", text);
      }
    });

    clearBtn.addEventListener("click", function () {
      clearSelected();
    });
  }

  function initPinnedUi() {
    const list = document.getElementById("chatPinnedList");
    if (!list) return;

    refreshPinnedPanelState();
    list.addEventListener("click", function (event) {
      const btn = event.target.closest(".chat-pinned-item");
      if (!btn) return;
      scrollToPinnedTarget(btn.getAttribute("data-message-id") || "", btn.getAttribute("data-attachment-id") || "");
    });
  }

  function initPinActions() {
    const box = document.getElementById("chatMessages");
    if (!box) return;

    box.addEventListener("click", async function (event) {
      const messageBtn = event.target.closest(".js-toggle-pin-message");
      if (messageBtn) {
        const messageId = messageBtn.getAttribute("data-message-id") || "";
        if (!messageId) return;
        try {
          const response = await fetch("/chat/api/messages/" + encodeURIComponent(messageId) + "/pin", {
            method: "POST",
            headers: {
              "X-Requested-With": "XMLHttpRequest",
              "Accept": "application/json"
            },
            credentials: "same-origin"
          });
          const data = await response.json().catch(function () { return null; });
          if (!response.ok || !data || !data.ok) {
            throw new Error((data && data.detail) || "Không cập nhật được trạng thái ghim.");
          }
          applyMessagePinState(messageId, !!data.is_pinned, data.pin_item || null);
        } catch (err) {
          window.alert(err && err.message ? err.message : "Không cập nhật được trạng thái ghim.");
        }
        return;
      }

      const attachmentBtn = event.target.closest(".js-toggle-pin-attachment");
      if (attachmentBtn) {
        const attachmentId = attachmentBtn.getAttribute("data-attachment-id") || "";
        const messageId = attachmentBtn.getAttribute("data-message-id") || "";
        if (!attachmentId) return;
        try {
          const response = await fetch("/chat/api/attachments/" + encodeURIComponent(attachmentId) + "/pin", {
            method: "POST",
            headers: {
              "X-Requested-With": "XMLHttpRequest",
              "Accept": "application/json"
            },
            credentials: "same-origin"
          });
          const data = await response.json().catch(function () { return null; });
          if (!response.ok || !data || !data.ok) {
            throw new Error((data && data.detail) || "Không cập nhật được trạng thái ghim file.");
          }
          applyAttachmentPinState(attachmentId, messageId || data.message_id || "", !!data.is_pinned, data.pin_item || null);
        } catch (err) {
          window.alert(err && err.message ? err.message : "Không cập nhật được trạng thái ghim file.");
        }
      }
    });
  }
  
  function initReactionActions() {
    const box = document.getElementById("chatMessages");
    if (!box) return;

    box.addEventListener("click", async function (event) {
      const btn = event.target.closest(".chat-reaction-btn");
      if (!btn) return;

      const bar = btn.closest(".chat-reaction-bar");
      const row = btn.closest(".chat-message-row");
      if (!bar || !row) return;

      const messageId = bar.getAttribute("data-message-id") || row.getAttribute("data-message-id") || "";
      const reactionType = btn.getAttribute("data-reaction") || "like";
      if (!messageId) return;

      const formData = new FormData();
      formData.set("reaction_type", reactionType);

      try {
        const response = await fetch("/chat/api/messages/" + encodeURIComponent(messageId) + "/react", {
          method: "POST",
          body: formData,
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json"
          },
          credentials: "same-origin"
        });

        const data = await response.json().catch(function () { return null; });
        if (!response.ok || !data || !data.ok) {
          throw new Error("Không cập nhật được reaction.");
        }

        updateReactionBar(messageId, data.reaction_counts || {});
      } catch (err) {
        window.alert(err && err.message ? err.message : "Không cập nhật được reaction.");
      }
    });
  }

  function initFileUpload() {
    const fileInput = document.getElementById("chatFileInput");
    const pickBtn = document.getElementById("chatFilePickerButton");
    const form = document.getElementById("chatComposerForm");
    const note = document.getElementById("chatComposerNote");
    const replyIdInput = document.getElementById("chatReplyToMessageId");

    if (!fileInput || !pickBtn || !form) return;

    function setNote(message, isError) {
      if (!note) return;
      note.textContent = message || "";
      note.classList.toggle("is-error", !!isError);
    }

    pickBtn.addEventListener("click", function () {
      fileInput.click();
    });

    fileInput.addEventListener("change", async function () {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;

      const groupIdInput = form.querySelector('input[name="group_id"]');
      const groupId = groupIdInput ? (groupIdInput.value || "").trim() : "";
      if (!groupId) {
        setNote("Không xác định được nhóm chat.", true);
        return;
      }

      const fd = new FormData();
      fd.set("group_id", groupId);
      fd.set("reply_to_message_id", replyIdInput ? (replyIdInput.value || "") : "");
      fd.set("file", file);

      setNote("Đang tải file lên...", false);

      try {
        const response = await fetch("/chat/api/messages/upload", {
          method: "POST",
          body: fd,
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json"
          },
          credentials: "same-origin"
        });

        const data = await response.json().catch(function () { return null; });
        if (!response.ok || !data || !data.ok || !data.message) {
          const detail = data && data.detail ? data.detail : "Không tải được file.";
          throw new Error(detail);
        }

        appendMessageToRoom(data.message);
        setNote("");
        fileInput.value = "";
        clearReplyState();
      } catch (err) {
        setNote(err && err.message ? err.message : "Không tải được file.", true);
      }
    });
  }

  function initStickerPicker() {
    const button = document.getElementById("chatStickerPickerButton");
    const panel = document.getElementById("chatStickerPanel");
    const closeButton = document.getElementById("chatStickerCloseButton");
    const list = document.getElementById("chatStickerList");
    const form = document.getElementById("chatComposerForm");
    const note = document.getElementById("chatComposerNote");
    const replyIdInput = document.getElementById("chatReplyToMessageId");

    if (!button || !panel || !list || !form) return;

    let stickersLoaded = false;
    let isSending = false;

    function setNote(message, isError) {
      if (!note) return;
      note.textContent = message || "";
      note.classList.toggle("is-error", !!isError);
    }

    function setPanelOpen(isOpen) {
      panel.hidden = !isOpen;
      button.classList.toggle("is-active", !!isOpen);
    }

    async function loadStickersOnce() {
      if (stickersLoaded) return;
      stickersLoaded = true;
      list.innerHTML = '<div class="chat-sticker-empty">Đang tải sticker...</div>';

      try {
        const response = await fetch("/chat/api/stickers", {
          method: "GET",
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json"
          },
          credentials: "same-origin"
        });
        const data = await response.json().catch(function () { return null; });
        if (!response.ok || !data || !data.ok) {
          throw new Error((data && data.detail) || "Không tải được danh sách sticker.");
        }

        const stickers = Array.isArray(data.stickers) ? data.stickers : [];
        if (!stickers.length) {
          list.innerHTML = '<div class="chat-sticker-empty">Chưa có sticker trong thư mục /static/sticker.</div>';
          return;
        }

        list.innerHTML = stickers.map(function (item) {
          const url = item && item.url ? String(item.url) : "";
          const name = item && item.name ? String(item.name) : "Sticker";
          if (!isSafeStickerUrl(url)) return "";
          return [
            '<button type="button" class="chat-sticker-item" data-sticker-path="' + escapeHtml(url) + '" title="' + escapeHtml(name) + '">',
            '<img src="' + escapeHtml(url) + '" alt="' + escapeHtml(name) + '">',
            '</button>'
          ].join("");
        }).join("");
      } catch (err) {
        stickersLoaded = false;
        list.innerHTML = '<div class="chat-sticker-empty is-error">Không tải được sticker.</div>';
        setNote(err && err.message ? err.message : "Không tải được sticker.", true);
      }
    }

    async function sendSticker(stickerPath) {
      if (isSending) return;
      if (!isSafeStickerUrl(stickerPath)) {
        setNote("Sticker không hợp lệ.", true);
        return;
      }

      const groupIdInput = form.querySelector('input[name="group_id"]');
      const groupId = groupIdInput ? (groupIdInput.value || "").trim() : "";
      if (!groupId) {
        setNote("Không xác định được nhóm chat.", true);
        return;
      }

      const fd = new FormData();
      fd.set("group_id", groupId);
      fd.set("sticker_path", stickerPath);
      fd.set("reply_to_message_id", replyIdInput ? (replyIdInput.value || "") : "");

      isSending = true;
      button.classList.add("is-loading");
      setNote("Đang gửi sticker...", false);

      try {
        const response = await fetch("/chat/api/messages/sticker", {
          method: "POST",
          body: fd,
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json"
          },
          credentials: "same-origin"
        });
        const data = await response.json().catch(function () { return null; });
        if (!response.ok) {
          const detail = data && data.detail ? data.detail : "Không gửi được sticker.";
          throw new Error(detail);
        }
        if (!data || !data.ok || !data.message) {
          throw new Error("Phản hồi gửi sticker không hợp lệ.");
        }

        appendMessageToRoom(data.message);
        setPanelOpen(false);
        setNote("", false);
        clearReplyState();
      } catch (err) {
        setNote(err && err.message ? err.message : "Không gửi được sticker.", true);
      } finally {
        isSending = false;
        button.classList.remove("is-loading");
      }
    }

    button.addEventListener("click", async function (event) {
      event.stopPropagation();
      const shouldOpen = !!panel.hidden;
      setPanelOpen(shouldOpen);
      if (shouldOpen) {
        await loadStickersOnce();
      }
    });

    if (closeButton) {
      closeButton.addEventListener("click", function (event) {
        event.stopPropagation();
        setPanelOpen(false);
      });
    }

    panel.addEventListener("click", function (event) {
      event.stopPropagation();
      const item = event.target.closest(".chat-sticker-item");
      if (!item) return;
      sendSticker(item.getAttribute("data-sticker-path") || "");
    });

    document.addEventListener("click", function () {
      if (!panel.hidden) {
        setPanelOpen(false);
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !panel.hidden) {
        setPanelOpen(false);
      }
    });
  }

  function initAjaxComposer() {
    const form = document.getElementById("chatComposerForm");
    const input = document.getElementById("chatComposerInput");
    const sendButton = document.getElementById("chatSendButton");
    const note = document.getElementById("chatComposerNote");

    if (!form || !input || !sendButton) return;

    function setNote(message, isError) {
      if (!note) return;
      note.textContent = message || "";
      note.classList.toggle("is-error", !!isError);
    }

    async function submitComposer() {
      const value = (input.value || "").trim();
      if (!value) {
        setNote("Nội dung tin nhắn không được để trống.", true);
        input.focus();
        return;
      }

      const formData = new FormData(form);
      formData.set("content", value);

      sendButton.classList.add("is-loading");
      sendButton.disabled = true;
      setNote("");

      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: formData,
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json"
          },
          credentials: "same-origin"
        });

        const data = await response.json().catch(function () { return null; });
        if (!response.ok) {
          const detail = data && data.detail ? data.detail : "Không gửi được tin nhắn.";
          throw new Error(detail);
        }
        if (!data || !data.ok || !data.message) {
          throw new Error("Phản hồi gửi tin nhắn không hợp lệ.");
        }

        appendMessageToRoom(data.message);
        input.value = "";
        input.focus();
        setNote("");
        clearReplyState();
      } catch (error) {
        setNote(error && error.message ? error.message : "Không gửi được tin nhắn.", true);
      } finally {
        sendButton.classList.remove("is-loading");
        sendButton.disabled = false;
      }
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      submitComposer();
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submitComposer();
      }
    });
  }

  function initGroupSocket() {
    const box = document.getElementById("chatMessages");
    const form = document.getElementById("chatComposerForm");
    if (!box || !form) return;

    const currentUserId = box.getAttribute("data-current-user-id") || "";
    const groupIdInput = form.querySelector('input[name="group_id"]');
    const groupId = groupIdInput ? (groupIdInput.value || "").trim() : "";
    if (!groupId) return;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = protocol + "//" + window.location.host + "/ws/chat/groups/" + encodeURIComponent(groupId);

    let socket = null;
    let reconnectTimer = null;

    function connect() {
      socket = new WebSocket(wsUrl);

      socket.onmessage = function (event) {
        let payload = null;
        try {
          payload = JSON.parse(event.data);
        } catch (err) {
          return;
        }

        if (!payload) return;
        
		if (payload.type === "attachment_recalled" && payload.attachment_id && payload.message_id) {
          applyAttachmentRecalled(
            payload.attachment_id,
            payload.message_id,
            payload.filename || "[Đã thu hồi] Tệp đính kèm",
            payload.content || ""
          );
          return;
        }

        if (payload.type === "attachment_deleted" && payload.attachment_id && payload.message_id) {
          applyAttachmentDeleted(
            payload.attachment_id,
            payload.message_id,
            payload.content || ""
          );
          return;
        }
		
        if (payload.type === "message_pin_toggled" && payload.message_id) {
          applyMessagePinState(payload.message_id, !!payload.is_pinned, payload.pin_item || null);
          return;
        }

        if (payload.type === "attachment_pin_toggled" && payload.attachment_id) {
          applyAttachmentPinState(payload.attachment_id, payload.message_id || "", !!payload.is_pinned, payload.pin_item || null);
          return;
        }
		
        if (payload.type === "new_message" && payload.message) {
          const message = payload.message;
          if ((message.sender_user_id || "") === currentUserId) {
            return;
          }

          appendMessageToRoom({
            id: message.id,
            group_id: message.group_id,
            sender_user_id: message.sender_user_id,
            sender_name: message.sender_name,
            content: message.content,
            message_type: message.message_type,
            recalled: !!message.recalled,
            created_at_text: message.created_at_text || "",
            is_mine: false,
            reply_preview: message.reply_preview || null,
            reaction_counts: message.reaction_counts || { like: 0, heart: 0, laugh: 0 },
            attachments: message.attachments || []
          });

          setGroupUnreadBadge(message.group_id || "", 0);
          markActiveGroupReadRealtime();
          return;
        }

        if (payload.type === "reaction_update" && payload.message_id) {
          updateReactionBar(payload.message_id, payload.reaction_counts || {});
          return;
        }

        if (payload.type === "message_recalled" && payload.message_id) {
          applyMessageRecalled(payload.message_id, payload.content || "Tin nhắn đã được thu hồi.");
          return;
        }

        if (payload.type === "message_deleted" && payload.message_id) {
          removeMessageFromRoom(payload.message_id);
        }
      };

      socket.onclose = function () {
        if (reconnectTimer) {
          window.clearTimeout(reconnectTimer);
        }
        reconnectTimer = window.setTimeout(connect, 1500);
      };

      socket.onerror = function () {
        try {
          socket.close();
        } catch (err) {
          // bỏ qua
        }
      };
    }

    connect();
  }

  function initNotifySocket() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = protocol + "//" + window.location.host + "/ws/chat/notify";

    let socket = null;
    let reconnectTimer = null;

    function connect() {
      socket = new WebSocket(wsUrl);

      socket.onmessage = function (event) {
        let payload = null;
        try {
          payload = JSON.parse(event.data);
        } catch (err) {
          return;
        }

        if (!payload) return;

        if (payload.type === "unread_update" && payload.group_id) {
          setGroupUnreadBadge(
            payload.group_id,
            Number(payload.new_message_count || 0)
          );
          if (typeof payload.is_new_group !== "undefined") {
            setGroupNewBadge(payload.group_id, !!payload.is_new_group);
          }
          dispatchSystemNotify(payload);
         return;
        }

        if (payload.type === "group_new_badge" && payload.group_id) {
          if (!hasGroupItemInDom(payload.group_id)) {
            showNewGroupSoftRefreshNotice(payload);
            return;
          }

          setGroupNewBadge(payload.group_id, !!payload.is_new_group);

          if (typeof payload.new_message_count !== "undefined") {
            setGroupUnreadBadge(
              payload.group_id,
              Number(payload.new_message_count || 0)
            );
          }
          dispatchSystemNotify(payload);
          return;
        }

        if (payload.module === "work" || payload.module === "draft") {
          dispatchSystemNotify(payload);
          return;
        }

        dispatchSystemNotify(payload);
      };

      socket.onclose = function () {
        if (reconnectTimer) {
          window.clearTimeout(reconnectTimer);
        }
        reconnectTimer = window.setTimeout(connect, 1500);
      };

      socket.onerror = function () {
        try {
          socket.close();
        } catch (err) {
          // bỏ qua
        }
      };
    }

    connect();
  }
  document.addEventListener("DOMContentLoaded", function () {
    initGroupSearch("groupSearchInput", "chatGroupList");
    initGroupSearch("roomGroupSearchInput", "roomChatGroupList");
    initCollapsible("homeGroupToggle", "homeGroupBox");
    initGroupDrawer();
    initMemberPanel();
    initConfirmForms();
    initScrollMessagesToBottom();
    initReplyUi();
    initCopyForwardActions();
    initBulkSelect();
    initPinnedUi();
    initPinActions();
    initReactionActions();
    initFileUpload();
    initStickerPicker();
    initAjaxComposer();

    const path = window.location.pathname || "";

    // CHỈ chạy socket khi ở chat hoặc meetings
    if (path.startsWith("/chat") || path.startsWith("/meetings")) {
      initGroupSocket();
      initNotifySocket();
    }
  });
})();
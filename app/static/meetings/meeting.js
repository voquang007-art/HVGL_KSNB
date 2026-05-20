// app/static/meetings/meeting.js
// Realtime/AJAX riêng cho phân hệ Họp trực tuyến.
// Không dùng chung chat.js vì meeting có DOM và nghiệp vụ riêng: điểm danh, báo vắng,
// xin rời họp, đăng ký phát biểu, tài liệu, kết luận, biên bản.

(function () {
  "use strict";

  const CFG = window.HVGL_MEETING_CONFIG || {};
  const state = {
    selectedId: String(CFG.selectedId || "").trim(),
    softRefreshTimer: null,
    socket: null,
    socketReconnectTimer: null,
    syncIntervalId: null,
    isRefreshing: false,
    isMeetingDeleted: false,
    messageSubmitted: false,
    presenceLeaveUrl: ""
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function qs(selector, root) {
    return (root || document).querySelector(selector);
  }

  function qsa(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function getSelectedId() {
    if (state.selectedId) return state.selectedId;

    const pane = byId("meetingDetailPane");
    if (pane) {
      state.selectedId = String(pane.getAttribute("data-selected-id") || "").trim();
    }

    if (!state.selectedId) {
      const actions = byId("meetingMainActions");
      if (actions) {
        state.selectedId = String(actions.getAttribute("data-group-id") || "").trim();
      }
    }

    return state.selectedId;
  }

  function draftKey() {
    const groupId = getSelectedId();
    return groupId ? "hvgl_ksnb_meeting_draft_" + groupId : "";
  }

  function getMessageTextarea() {
    return byId("meetingMessageTextarea");
  }

  function saveDraft() {
    const textarea = getMessageTextarea();
    const key = draftKey();
    if (!textarea || !key) return;
    localStorage.setItem(key, textarea.value || "");
  }

  function restoreDraft() {
    const textarea = getMessageTextarea();
    const key = draftKey();
    if (!textarea || !key) return;

    const saved = localStorage.getItem(key) || "";
    if (saved && !textarea.value) {
      textarea.value = saved;
    }
  }

  function clearDraft() {
    const key = draftKey();
    if (key) localStorage.removeItem(key);
  }

  function setInlineNotice(message, isError) {
    const pane = byId("meetingDetailPane") || document.body;
    let notice = byId("meetingRealtimeNotice");
    if (!notice) {
      notice = document.createElement("div");
      notice.id = "meetingRealtimeNotice";
      notice.className = "meeting-card";
      notice.style.marginBottom = "12px";
      pane.insertBefore(notice, pane.firstChild || null);
    }

    notice.textContent = message || "";
    notice.classList.toggle("meeting-alert-err", !!isError);
    notice.classList.toggle("meeting-alert-ok", !isError);
    notice.hidden = !message;

    if (message) {
      window.setTimeout(function () {
        const current = byId("meetingRealtimeNotice");
        if (current) current.hidden = true;
      }, 3500);
    }
  }

  function isTypingInMeetingInput() {
    const activeEl = document.activeElement;
    return !!(
      activeEl &&
      (activeEl.tagName === "TEXTAREA" || activeEl.tagName === "INPUT") &&
      activeEl.closest("#meetingDetailPane")
    );
  }

  function updateSelectedIdFromDom() {
    const pane = byId("meetingDetailPane");
    const nextId = pane ? String(pane.getAttribute("data-selected-id") || "").trim() : "";
    if (nextId && nextId !== state.selectedId) {
      state.selectedId = nextId;
    }
  }

  function stopMeetingRuntime() {
    state.isMeetingDeleted = true;

    window.clearTimeout(state.softRefreshTimer);
    state.softRefreshTimer = null;

    window.clearTimeout(state.socketReconnectTimer);
    state.socketReconnectTimer = null;

    if (state.syncIntervalId) {
      window.clearInterval(state.syncIntervalId);
      state.syncIntervalId = null;
    }

    if (state.socket) {
      try { state.socket.close(); } catch (err) {}
      state.socket = null;
    }

    state.presenceLeaveUrl = "";
  }

  function isCurrentMeetingDeletedPayload(payload) {
    if (!payload || String(payload.type || "") !== "meeting_deleted") return false;

    const groupId = getSelectedId();
    if (!groupId) return true;

    return !payload.group_id || String(payload.group_id) === groupId;
  }

  function handleDeletedMeeting(payload) {
    if (!isCurrentMeetingDeletedPayload(payload)) return false;

    stopMeetingRuntime();

    if (payload && payload.deleted_by_user_id && CFG.currentUserId && String(payload.deleted_by_user_id) === String(CFG.currentUserId)) {
      return true;
    }

    const redirectUrl = (payload && payload.redirect_url) ? String(payload.redirect_url) : "/meetings";
    window.location.href = redirectUrl;
    return true;
  }

  function isDeletingCurrentMeetingForm(form) {
    if (!form) return false;

    const groupId = getSelectedId();
    if (!groupId) return false;

    const action = String(form.getAttribute("action") || "");
    return action.indexOf("/meetings/" + groupId + "/delete") >= 0;
  }
  
  function replaceFromFetchedHtml(html) {
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, "text/html");

    const nextSidebar = doc.getElementById("meetingSidebar");
    const curSidebar = byId("meetingSidebar");
    if (nextSidebar && curSidebar) {
      curSidebar.replaceWith(nextSidebar);
    }

    const nextPane = doc.getElementById("meetingDetailPane");
    const curPane = byId("meetingDetailPane");
    if (nextPane && curPane) {
      curPane.replaceWith(nextPane);
    }

    updateSelectedIdFromDom();
    restoreDraft();
    initParticipantMultiselect();
    initCreateTimeConfirmButton();
  }

  function softRefreshMeeting(reason) {
    const groupId = getSelectedId();
    if (!groupId || state.isRefreshing || state.isMeetingDeleted) return Promise.resolve(null);

    saveDraft();
    state.isRefreshing = true;

    return fetch("/meetings?selected_id=" + encodeURIComponent(groupId) + "&_ts=" + Date.now(), {
      method: "GET",
      credentials: "same-origin",
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        "Cache-Control": "no-cache"
      }
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("Không tải được dữ liệu cuộc họp mới nhất.");
        }
        return response.text();
      })
      .then(function (html) {
        replaceFromFetchedHtml(html);
        if (reason) setInlineNotice(reason, false);
        return true;
      })
      .catch(function (err) {
        setInlineNotice(err && err.message ? err.message : "Không cập nhật được dữ liệu cuộc họp.", true);
        return false;
      })
      .finally(function () {
        state.isRefreshing = false;
      });
  }

  function requestSoftRefresh(reason, delayMs) {
    if (state.isMeetingDeleted) return;

    window.clearTimeout(state.softRefreshTimer);
    state.softRefreshTimer = window.setTimeout(function () {
      softRefreshMeeting(reason || "Cuộc họp đã được cập nhật.");
    }, typeof delayMs === "number" ? delayMs : 150);
  }

  function initParticipantMultiselect() {
    const scopeSelect = byId("meetingScopeSelect");
    const multi = byId("participantMultiSelect");
    const btn = byId("participantMultiSelectBtn");
    const label = byId("participantMultiSelectLabel");
    const panel = byId("participantMultiSelectPanel");
    const search = byId("participantMultiSelectSearch");
    const list = byId("participantMultiSelectList");
    const confirmBtn = byId("participantMultiSelectConfirm");
    const countText = byId("participantMultiSelectCount");
    const hostSelect = byId("meetingHostSelect");
    const secretarySelect = byId("meetingSecretarySelect");

    if (!multi || !btn || !panel || !list || btn.dataset.meetingBound === "1") return;
    btn.dataset.meetingBound = "1";

    function selectedScope() {
      return scopeSelect ? String(scopeSelect.value || "").trim().toUpperCase() : "";
    }

    function optionAllowsScope(el, scope) {
      const scopes = String(el.getAttribute("data-scopes") || "")
        .split(",")
        .map(function (s) { return s.trim().toUpperCase(); })
        .filter(Boolean);
      return !scope || scopes.indexOf(scope) >= 0;
    }

    function refreshLabel() {
      const checked = qsa("input[type='checkbox']:checked", list);
      if (!label) return;

      if (!checked.length) {
        label.textContent = "-- Chọn thành phần dự họp --";
        if (countText) countText.textContent = "Chưa chọn thành viên";
        return;
      }

      if (checked.length === 1) {
        const row = checked[0].closest("label");
        label.textContent = row ? row.innerText.trim() : "Đã chọn 1 thành viên";
        if (countText) countText.textContent = "Đã chọn 1 thành viên";
        return;
      }

      label.textContent = "Đã chọn " + checked.length + " người";
      if (countText) countText.textContent = "Đã chọn " + checked.length + " thành viên";
    }

    function refreshOptions() {
      const scope = selectedScope();
      const keyword = search ? String(search.value || "").trim().toLowerCase() : "";
      const options = qsa(".meeting-multiselect-option", list);

      options.forEach(function (opt) {
        const text = String(opt.getAttribute("data-label") || opt.textContent || "").toLowerCase();
        const allow = optionAllowsScope(opt, scope) && (!keyword || text.indexOf(keyword) !== -1);
        opt.style.display = allow ? "" : "none";
        if (!allow) {
          const cb = qs("input[type='checkbox']", opt);
          if (cb) cb.checked = false;
        }
      });

      [hostSelect, secretarySelect].forEach(function (sel) {
        if (!sel) return;
        qsa("option", sel).forEach(function (opt) {
          if (!opt.value) {
            opt.hidden = false;
            return;
          }
          const scopes = String(opt.getAttribute("data-scopes") || "")
            .split(",")
            .map(function (s) { return s.trim().toUpperCase(); })
            .filter(Boolean);
          const allow = !scope || scopes.indexOf(scope) >= 0;
          opt.hidden = !allow;
          if (!allow && opt.selected) sel.value = "";
        });
      });

      refreshLabel();
    }

    btn.addEventListener("click", function (event) {
      event.preventDefault();
      multi.classList.toggle("open");
      if (multi.classList.contains("open") && search) {
        window.setTimeout(function () { search.focus(); }, 30);
      }
    });

    if (confirmBtn) {
      confirmBtn.addEventListener("click", function (event) {
        event.preventDefault();
        refreshLabel();
        multi.classList.remove("open");
      });
    }

    if (search) search.addEventListener("input", refreshOptions);
    if (scopeSelect) scopeSelect.addEventListener("change", refreshOptions);

    list.addEventListener("change", function (event) {
      if (event.target && event.target.matches("input[type='checkbox']")) {
        refreshLabel();
      }
    });

    document.addEventListener("click", function (event) {
      if (!multi.contains(event.target)) {
        multi.classList.remove("open");
      }
    });

    refreshOptions();
  }

  function initCreateTimeConfirmButton() {
    const button = byId("meetingCreateTimeConfirmBtn");
    if (!button || button.dataset.meetingBound === "1") return;
    button.dataset.meetingBound = "1";

    const form = button.closest("form");
    if (!form) return;

    function closeNativeDatetimePickers() {
      qsa("input[type='datetime-local']", form).forEach(function (input) {
        input.blur();
      });
    }

    qsa("input[type='datetime-local']", form).forEach(function (input) {
      if (input.dataset.meetingTimeBound === "1") return;
      input.dataset.meetingTimeBound = "1";

      input.addEventListener("change", function () {
        window.setTimeout(function () {
          input.blur();
        }, 80);
      });
    });

    button.addEventListener("mousedown", function () {
      closeNativeDatetimePickers();
    });

    button.addEventListener("click", function (event) {
      event.preventDefault();
      closeNativeDatetimePickers();
      setInlineNotice("Đã ghi nhận thời gian đã chọn trên form. Bấm Tạo phòng họp để lưu cuộc họp.", false);
    });
  }

  function meetingFetch(url, options) {
    const finalOptions = Object.assign({
      credentials: "same-origin",
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8"
      }
    }, options || {});

    return fetch(url, finalOptions).then(function (response) {
      if (!response.ok) {
        return response.text().then(function (text) {
          const err = new Error(text || "Thao tác không thực hiện được.");
          err.status = response.status;
          err.url = url;
          throw err;
        });
      }
      const contentType = response.headers.get("content-type") || "";
      if (contentType.indexOf("application/json") >= 0) {
        return response.json();
      }
      return response.text();
    });
  }

  function postPresence(path) {
    if (!path || state.isMeetingDeleted) return;
    meetingFetch(path, { method: "POST" }).catch(function (err) {
      if (err && err.status === 404) {
        stopMeetingRuntime();
      }
    });
  }

  function syncMeetingStatus() {
    const groupId = getSelectedId();
    if (!groupId || state.isMeetingDeleted) return Promise.resolve(null);

    return meetingFetch("/meetings/" + encodeURIComponent(groupId) + "/sync", {
      method: "POST"
    })
      .then(function (data) {
        if (!data || typeof data !== "object") return data;

        if (handleDeletedMeeting(data)) {
          return data;
        }

        if (data.removed_from_meeting && data.redirect_url) {
          stopMeetingRuntime();
          window.location.href = data.redirect_url;
          return data;
        }

        renderMeetingActionButtons(data);
        renderSpeakerControl(data);
        renderMeetingSendState(data);
        updateStatusLabel(data);
        return data;
      })
      .catch(function (err) {
        if (err && err.status === 404) {
          stopMeetingRuntime();
          window.location.href = "/meetings";
        }
        return null;
      });
  }

  function updateStatusLabel(data) {
    if (!data || !data.meeting_status_label) return;
    const pane = byId("meetingDetailPane");
    if (!pane) return;

    qsa(".meeting-pill", pane).forEach(function (pill) {
      const text = (pill.textContent || "").trim();
      if (text === "Sắp họp" || text === "Đang họp" || text === "Đã kết thúc") {
        pill.textContent = data.meeting_status_label;
        pill.classList.toggle("live", data.meeting_status === "LIVE");
        pill.classList.toggle("ended", data.meeting_status === "ENDED");
      }
    });
  }

  function minutesLinkHtml(groupId, canExport) {
    if (!canExport) return "";
    return '<a class="meeting-btn secondary" href="/meetings/' + encodeURIComponent(groupId) + '/minutes.txt">Xuất biên bản TXT</a>';
  }

  function renderMeetingActionButtons(data) {
    const actions = byId("meetingMainActions");
    if (!actions || !data) return;

    const groupId = actions.getAttribute("data-group-id") || getSelectedId();
    const minutesLink = minutesLinkHtml(groupId, data.can_export_minutes === true);
    let html = "";

    if (data.action_mode === "checked_in") {
      html += '<span class="meeting-pill live">Đã điểm danh</span>';
    } else if (data.action_mode === "checkin") {
      html += '<form method="post" action="/meetings/' + encodeURIComponent(groupId) + '/checkin" data-meeting-action="checkin">' +
        '<button class="meeting-btn" type="submit">Điểm danh</button>' +
        '</form>';
    } else if (data.action_mode === "absent") {
      html += '<form method="post" action="/meetings/' + encodeURIComponent(groupId) + '/absent" data-meeting-action="absent">' +
        '<input type="hidden" name="reason" value="">' +
        '<button class="meeting-btn warn" type="submit">Báo vắng</button>' +
        '</form>';
    } else if (data.action_mode === "absent_cancel") {
      html += '<form method="post" action="/meetings/' + encodeURIComponent(groupId) + '/absent/cancel" data-meeting-action="absent-cancel">' +
        '<button class="meeting-btn secondary" type="submit">Hủy báo vắng</button>' +
        '</form>';
    }

    if (data.can_request_leave === true) {
      html += '<form method="post" action="/meetings/' + encodeURIComponent(groupId) + '/leave/request" data-meeting-action="leave-request">' +
        '<input type="hidden" name="note" value="">' +
        '<button class="meeting-btn warn" type="submit">Xin phép rời cuộc họp</button>' +
        '</form>';
    } else if (data.has_pending_leave_request === true) {
      html += '<span class="meeting-pill">Đã xin phép rời họp, chờ Chủ trì cho phép</span>';
    }

    html += minutesLink;
    actions.innerHTML = html;
  }

  function renderSpeakerControl(data) {
    const box = byId("meetingSpeakerControlBox");
    if (!box || !data) return;

    const registerUrl = box.getAttribute("data-register-url") || "";
    if (data.can_register_speaker === true) {
      box.innerHTML =
        '<form method="post" action="' + escapeHtml(registerUrl) + '" style="margin-bottom:12px;" id="meetingSpeakerFallbackForm">' +
          '<input type="hidden" name="note" value="Tôi xin phát biểu.">' +
          '<button class="meeting-btn gold" type="submit" id="meetingSpeakerOpenBtn" data-speaker-register-url="' + escapeHtml(registerUrl) + '">Đăng ký phát biểu</button>' +
        '</form>' +
        '<div class="meeting-modal-backdrop" id="meetingSpeakerModal" aria-hidden="true">' +
          '<div class="meeting-modal" role="dialog" aria-modal="true" aria-labelledby="meetingSpeakerModalTitle">' +
            '<h4 class="meeting-modal-title" id="meetingSpeakerModalTitle">Đăng ký phát biểu</h4>' +
            '<div class="meeting-modal-note">Nhập ngắn gọn nội dung đăng ký để Chủ trì biết anh/chị xin phát biểu. Ô “Nội dung trao đổi” bên trái vẫn được giữ nguyên, không bị mất khi gửi đăng ký.</div>' +
            '<div class="meeting-field"><label>Nội dung đăng ký</label><textarea id="meetingSpeakerNoteTextarea" placeholder="Ví dụ: Tôi xin phát biểu.">Tôi xin phát biểu.</textarea></div>' +
            '<div class="meeting-modal-actions">' +
              '<button type="button" class="meeting-btn secondary" id="meetingSpeakerCancelBtn">Hủy</button>' +
              '<button type="button" class="meeting-btn gold" id="meetingSpeakerSubmitBtn">Gửi đăng ký</button>' +
            '</div>' +
          '</div>' +
        '</div>';
      return;
    }

    if (data.has_pending_speaker_request === true) {
      box.innerHTML = '<div class="meeting-empty" style="margin-bottom:12px;">Anh/chị đã đăng ký phát biểu, đang chờ Chủ trì cho phép.</div>';
      return;
    }

    if (data.is_host === true) {
      box.innerHTML = '<div class="meeting-note" style="margin-bottom:12px;">Chủ trì không cần đăng ký phát biểu. Chủ trì xử lý các yêu cầu phát biểu bên dưới.</div>';
      return;
    }

    box.innerHTML = "";
  }

  function renderMeetingSendState(data) {
    const form = byId("meetingMessageForm");
    if (!form || !data) return;

    const submitButton = qs("button[type='submit']", form);
    if (submitButton) {
      submitButton.disabled = data.can_send_meeting_message !== true;
    }
  }

  function renderMeetingMessage(message) {
    if (!message || !message.id) return;
    const list = byId("meetingChatList") || qs(".meeting-chat-list");
    if (!list) return;

    if (qs('.meeting-chat-item[data-message-id="' + CSS.escape(message.id) + '"]', list)) {
      return;
    }

    qsa(".meeting-empty", list).forEach(function (node) { node.remove(); });

    const attachments = Array.isArray(message.attachments) ? message.attachments : [];
    const attachmentHtml = attachments.length
      ? '<div class="meeting-doc-actions" style="margin-top:8px;">' + attachments.map(function (att) {
          const filename = escapeHtml(att.filename || "Tệp đính kèm");
          const previewUrl = escapeHtml(att.preview_url || "#");
          const downloadUrl = escapeHtml(att.download_url || "#");
          return (att.is_previewable ? '<a class="meeting-btn soft-blue" href="' + previewUrl + '" target="_blank" rel="noopener">Xem tài liệu</a>' : '') +
            '<a class="meeting-btn soft-blue" href="' + downloadUrl + '">Tải về</a>';
        }).join("") + '</div>'
      : "";

    const html =
      '<div class="meeting-chat-item ' + (message.is_mine ? 'mine' : '') + '" data-message-id="' + escapeHtml(message.id) + '">' +
        '<div class="meeting-chat-meta">' + escapeHtml(message.sender_name || "Người dùng") + ' • ' + escapeHtml(message.created_at_text || "") + '</div>' +
        '<div>' + escapeHtml(message.content || "").replace(/\n/g, "<br>") + '</div>' +
        attachmentHtml +
      '</div>';

    list.insertAdjacentHTML("beforeend", html);
    list.scrollTop = list.scrollHeight;
  }

  function renderAttendanceRows(payload) {
    if (!payload || !payload.user_id) return;
    const row = qs('#meetingAttendanceTableBody tr[data-user-id="' + CSS.escape(String(payload.user_id)) + '"]');
    if (!row) return;

    const pill = qs(".meeting-attendance-status-pill", row) || qs(".meeting-pill", row);
    if (pill && payload.attendance_status_label) {
      pill.textContent = payload.attendance_status_label;
    }

    const presence = qs(".meeting-presence-status-text", row);
    if (presence && payload.presence_status_label) {
      presence.textContent = payload.presence_status_label;
    }
  }

  function renderLeaveRequests(payload) {
    if (!payload || !payload.type) return;
    if (payload.type === "meeting_leave_requested") {
      requestSoftRefresh("Có yêu cầu xin rời cuộc họp mới.", 100);
      return;
    }
    if (payload.type === "meeting_leave_approved" || payload.type === "meeting_leave_rejected") {
      requestSoftRefresh("Yêu cầu xin rời cuộc họp đã được cập nhật.", 100);
    }
  }

  function renderMeetingDocuments(payload) {
    if (!payload || payload.type !== "meeting_document_uploaded") return;
    const list = byId("meetingDocumentList") || qs(".meeting-doc-list");
    if (!list || !payload.attachment_id) {
      requestSoftRefresh("Tài liệu cuộc họp đã được cập nhật.", 100);
      return;
    }

    qsa(".meeting-note", list).forEach(function (node) {
      if ((node.textContent || "").indexOf("Chưa có tài liệu") >= 0) node.remove();
    });

    const filename = escapeHtml(payload.filename || "Tài liệu cuộc họp");
    const attId = encodeURIComponent(payload.attachment_id);
    const html =
      '<div class="meeting-doc-item" data-attachment-id="' + escapeHtml(payload.attachment_id) + '">' +
        '<div class="meeting-doc-title">' + filename + '</div>' +
        '<div class="meeting-doc-meta">Vừa tải lên</div>' +
        '<div class="meeting-doc-actions">' +
          '<a class="meeting-btn soft-blue" href="/meetings/attachments/' + attId + '/preview" target="_blank" rel="noopener">Xem tài liệu</a>' +
          '<a class="meeting-btn soft-blue" href="/meetings/attachments/' + attId + '/download">Tải về</a>' +
        '</div>' +
      '</div>';
    list.insertAdjacentHTML("afterbegin", html);
  }

  function renderConclusion(payload) {
    if (!payload || payload.type !== "meeting_conclusion_saved") return;
    const box = byId("meetingConclusionBox");
    if (!box) {
      requestSoftRefresh("Kết luận cuộc họp đã được cập nhật.", 100);
      return;
    }

    const display = byId("meetingConclusionDisplay");
    if (display) display.textContent = payload.conclusion_text || "";

    const updated = byId("meetingConclusionUpdatedText");
    if (updated && payload.conclusion_updated_text) {
      updated.textContent = "Cập nhật gần nhất: " + payload.conclusion_updated_text;
    }
  }

  function openSpeakerModal() {
    const modal = byId("meetingSpeakerModal");
    const textarea = byId("meetingSpeakerNoteTextarea");
    if (!modal || !textarea) return;

    if (!String(textarea.value || "").trim()) {
      textarea.value = "Tôi xin phát biểu.";
    }

    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    window.setTimeout(function () {
      textarea.focus();
      textarea.select();
    }, 30);
  }

  function closeSpeakerModal() {
    const modal = byId("meetingSpeakerModal");
    if (!modal) return;
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  }

  function submitSpeakerRegister(registerUrl, note) {
    if (!registerUrl) return Promise.resolve(null);
    const fd = new FormData();
    fd.append("note", note || "Tôi xin phát biểu.");

    return meetingFetch(registerUrl, {
      method: "POST",
      body: fd
    }).then(function (data) {
      closeSpeakerModal();
      requestSoftRefresh("Đã gửi đăng ký phát biểu, chờ Chủ trì cho phép.", 80);
      return data;
    }).catch(function (err) {
      window.alert(err && err.message ? err.message : "Không gửi được đăng ký phát biểu.");
      return null;
    });
  }

  function initDelegatedClicks() {
    document.addEventListener("submit", function (event) {
      if (isDeletingCurrentMeetingForm(event.target)) {
        stopMeetingRuntime();
      }
    }, true);

    document.addEventListener("click", function (event) {
      const openBtn = event.target.closest("#meetingSpeakerOpenBtn");
      if (openBtn) {
        event.preventDefault();
        saveDraft();
        openSpeakerModal();
        return;
      }

      if (event.target.closest("#meetingSpeakerCancelBtn")) {
        event.preventDefault();
        closeSpeakerModal();
        return;
      }

      const modal = byId("meetingSpeakerModal");
      if (modal && event.target === modal) {
        closeSpeakerModal();
        return;
      }

      const submitBtn = event.target.closest("#meetingSpeakerSubmitBtn");
      if (submitBtn) {
        event.preventDefault();
        const openButton = byId("meetingSpeakerOpenBtn");
        const registerUrl = openButton ? String(openButton.getAttribute("data-speaker-register-url") || "") : (byId("meetingSpeakerControlBox") || {}).dataset.registerUrl;
        const textarea = byId("meetingSpeakerNoteTextarea");
        const note = textarea ? String(textarea.value || "").trim() : "Tôi xin phát biểu.";
        submitBtn.disabled = true;
        submitSpeakerRegister(registerUrl, note || "Tôi xin phát biểu.").finally(function () {
          submitBtn.disabled = false;
        });
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeSpeakerModal();
    });
  }

  function shouldAjaxForm(form) {
    if (!form || !getSelectedId()) return false;
    const action = String(form.getAttribute("action") || "");
    if (!action) return false;
    if (form.closest("#meetingSidebar")) return false;
    if (action.indexOf("/meetings/" + getSelectedId() + "/delete") >= 0) return false;
    return action.indexOf("/meetings/" + getSelectedId() + "/") >= 0;
  }

  function prepareFormBeforeSubmit(form) {
    const actionMode = form.getAttribute("data-meeting-action") || "";
    if (actionMode === "absent") {
      const input = qs("input[name='reason']", form);
      if (input && !input.value) {
        const reason = window.prompt("Nhập lý do báo vắng cuộc họp:", "") || "";
        input.value = reason;
      }
    }
    if (actionMode === "leave-request") {
      const input = qs("input[name='note']", form);
      if (input && !input.value) {
        const note = window.prompt("Nhập lý do xin phép rời cuộc họp:", "") || "";
        input.value = note;
      }
    }
  }

  function initMeetingAjaxForms() {
    document.addEventListener("submit", function (event) {
      const form = event.target;
      if (!shouldAjaxForm(form)) return;

      event.preventDefault();
      prepareFormBeforeSubmit(form);

      const isMessageForm = form.id === "meetingMessageForm";
      const isSpeakerFallback = form.id === "meetingSpeakerFallbackForm";
      const submitButton = qs("button[type='submit']", form);

      if (submitButton) submitButton.disabled = true;

      if (isMessageForm) {
        state.messageSubmitted = true;
        clearDraft();
      } else {
        saveDraft();
      }

      meetingFetch(form.action, {
        method: String(form.method || "POST").toUpperCase(),
        body: new FormData(form)
      })
        .then(function (data) {
          if (isMessageForm) {
            const textarea = getMessageTextarea();
            if (textarea) textarea.value = "";
            clearDraft();
            window.setTimeout(syncMeetingStatus, 200);
            return;
          }

          if (isSpeakerFallback && data && typeof data === "object" && data.ok === false) {
            throw new Error(data.detail || "Không gửi được đăng ký phát biểu.");
          }

          requestSoftRefresh("Thao tác đã được cập nhật.", 80);
        })
        .catch(function (err) {
          window.alert(err && err.message ? err.message : "Không thực hiện được thao tác.");
        })
        .finally(function () {
          if (submitButton) submitButton.disabled = false;
          if (isMessageForm) state.messageSubmitted = false;
        });
    });
  }

  function handleMeetingPayload(payload) {
    if (!payload || typeof payload !== "object") return;

    if (handleDeletedMeeting(payload)) {
      return;
    }

    const type = String(payload.type || "");
    if (!type) return;

    if (payload.group_id && getSelectedId() && String(payload.group_id) !== getSelectedId()) {
      return;
    }

    if (type === "new_message") {
      renderMeetingMessage(payload.message || null);
      renderMeetingSendState(payload);
      window.setTimeout(syncMeetingStatus, 150);
      return;
    }

    if (type === "meeting_conclusion_saved") {
      renderConclusion(payload);
      requestSoftRefresh("Kết luận cuộc họp đã được cập nhật.", 300);
      return;
    }

    if (type === "meeting_document_uploaded") {
      renderMeetingDocuments(payload);
      requestSoftRefresh("Tài liệu cuộc họp đã được cập nhật.", 400);
      return;
    }

    if (
      type === "meeting_checkin_done" ||
      type === "meeting_absent_reported" ||
      type === "meeting_absent_cancelled" ||
      type === "meeting_presence_joined" ||
      type === "meeting_presence_left"
    ) {
      renderAttendanceRows(payload);
      syncMeetingStatus();
      requestSoftRefresh("Trạng thái tham dự đã được cập nhật.", 400);
      return;
    }

    if (
      type === "meeting_leave_requested" ||
      type === "meeting_leave_approved" ||
      type === "meeting_leave_rejected"
    ) {
      renderLeaveRequests(payload);
      return;
    }

    if (
      type === "meeting_speaker_registered" ||
      type === "meeting_speaker_approved" ||
      type === "meeting_speaker_reordered" ||
      type === "meeting_host_updated" ||
      type === "meeting_secretary_updated" ||
      type === "meeting_schedule_updated" ||
      type === "meeting_start_time_adjusted" ||
      type === "meeting_end_time_adjusted" ||
      type === "meeting_start_end_time_adjusted" ||
      type === "meeting_status_sync" ||
      type === "meeting_invited"
    ) {
      requestSoftRefresh("Cuộc họp đã được cập nhật.", 100);
    }
  }

  function initMeetingGroupSocket() {
    const groupId = getSelectedId();
    if (!groupId || !window.WebSocket || state.isMeetingDeleted) return;

    if (state.socket) {
      try { state.socket.close(); } catch (err) {}
      state.socket = null;
    }

    const protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
    const socketUrl = protocol + window.location.host + "/ws/chat/groups/" + encodeURIComponent(groupId);

    try {
      const ws = new WebSocket(socketUrl);
      state.socket = ws;

      ws.onmessage = function (event) {
        let payload = null;
        try {
          payload = JSON.parse(event.data || "{}");
        } catch (err) {
          return;
        }
        handleMeetingPayload(payload);
      };

      ws.onclose = function () {
        if (state.socket === ws) state.socket = null;
        if (state.isMeetingDeleted) return;
        window.clearTimeout(state.socketReconnectTimer);
        state.socketReconnectTimer = window.setTimeout(initMeetingGroupSocket, 3000);
      };
    } catch (err) {
      if (state.isMeetingDeleted) return;
      window.clearTimeout(state.socketReconnectTimer);
      state.socketReconnectTimer = window.setTimeout(initMeetingGroupSocket, 5000);
    }
  }

  function initMeetingNotifySocket() {
    // Hiện chưa mở socket notify riêng để tránh trùng với notify socket đã có ở base/chat.js.
    // Meeting dùng group socket /ws/chat/groups/<group_id> là kênh chính.
  }

  function initDraftHandling() {
    restoreDraft();

    document.addEventListener("input", function (event) {
      if (event.target && event.target.id === "meetingMessageTextarea") {
        localStorage.setItem(draftKey(), event.target.value || "");
      }
    });

    window.addEventListener("pagehide", function () {
      if (!state.messageSubmitted) saveDraft();
    });

    window.addEventListener("beforeunload", function () {
      if (!state.messageSubmitted) saveDraft();
    });
  }

  function initPresenceAndSync() {
    const groupId = getSelectedId();
    if (!groupId) return;

    state.presenceLeaveUrl = "/meetings/" + encodeURIComponent(groupId) + "/presence/leave";

    postPresence("/meetings/" + encodeURIComponent(groupId) + "/presence/join");
    syncMeetingStatus();

    window.addEventListener("focus", function () {
      if (!state.isMeetingDeleted) {
        syncMeetingStatus();
      }
    });

    state.syncIntervalId = window.setInterval(function () {
      if (!state.isMeetingDeleted && !isTypingInMeetingInput()) {
        syncMeetingStatus();
      }
    }, 3000);

    window.addEventListener("beforeunload", function () {
      if (state.isMeetingDeleted || !state.presenceLeaveUrl) return;

      try {
        navigator.sendBeacon(state.presenceLeaveUrl);
      } catch (err) {}
    });
  }

  function init() {
    updateSelectedIdFromDom();
    initParticipantMultiselect();
    initCreateTimeConfirmButton();
    initDraftHandling();
    initDelegatedClicks();
    initMeetingAjaxForms();
    initMeetingGroupSocket();
    initMeetingNotifySocket();
    initPresenceAndSync();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
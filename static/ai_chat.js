// IVY AI Assistant — multi-conversation chat interactions.
(function initializeAiAssistant() {
    const chatBox = document.getElementById("aiChatBox");
    const form = document.getElementById("aiChatForm");
    const textarea = document.getElementById("aiMessageText");
    const sendButton = document.getElementById("aiSendBtn");
    const thinkingIndicator = document.getElementById("ivyThinking");
    const conversationInput = document.getElementById("aiConversationId");
    const headerTitle = document.getElementById("aiHeaderTitle");

    const scrollToLatest = () => {
        if (chatBox) chatBox.scrollTop = chatBox.scrollHeight;
    };

    const getCsrfToken = () =>
        (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

    scrollToLatest();

    if (!form || !textarea || !conversationInput) return;

    const conversationId = conversationInput.value;
    let pendingRenameTarget = conversationId;

    const setBusy = (busy) => {
        if (sendButton) sendButton.disabled = busy;
        if (thinkingIndicator) thinkingIndicator.hidden = !busy;
    };

    const appendMessageRow = (payload) => {
        if (typeof createMessageElement !== "function") return null;
        const welcome = chatBox && chatBox.querySelector(".conversation-welcome");
        if (welcome) welcome.remove();
        const row = createMessageElement(payload, "self");
        chatBox.appendChild(row);
        scrollToLatest();
        return row;
    };

    const sidebarItem = () =>
        document.querySelector(`.conversation-item[data-conversation-id="${conversationId}"]`);

    const updateSidebarAfterSend = (previewText, isoTime) => {
        const item = sidebarItem();
        if (!item) return;

        const preview = item.querySelector(".conversation-preview");
        if (preview) preview.textContent = previewText;

        const time = item.querySelector("time");
        if (time) {
            time.dateTime = isoTime || "";
            time.textContent = typeof formatChatTime === "function" ? formatChatTime(isoTime) : "";
        }

        const list = item.parentElement;
        if (list) list.prepend(item);
    };

    const applyGeneratedTitle = (title, isoTime) => {
        if (!title) return;
        if (headerTitle) headerTitle.textContent = title;

        const item = sidebarItem();
        if (item) {
            item.dataset.conversationName = title.toLowerCase();
            const nameEl = item.querySelector(".conversation-name-row strong");
            if (nameEl) nameEl.textContent = title;
            if (isoTime) {
                const time = item.querySelector("time");
                if (time) {
                    time.dateTime = isoTime;
                    time.textContent = typeof formatChatTime === "function" ? formatChatTime(isoTime) : "";
                }
            }
        }
    };

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const content = textarea.value.trim();
        if (!content || form.dataset.busy === "1") return;

        form.dataset.busy = "1";
        setBusy(true);
        textarea.value = "";
        textarea.style.height = "auto";

        appendMessageRow({
            sender_id: "self",
            sender_name: "You",
            message_type: "text",
            message_text: content,
            created_at: new Date().toISOString(),
        });

        try {
            const response = await fetch(`/ai/conversations/${conversationId}/message`, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({ content }),
            });
            const result = await response.json().catch(() => ({}));

            if (!response.ok || !result.ok) {
                showChatNotice(result.error || "IVY is temporarily unavailable. Please try again.");
            } else {
                appendMessageRow({
                    sender_id: null,
                    sender_name: "IVY",
                    message_type: "ai",
                    message_text: result.reply.content,
                    created_at: result.reply.created_at,
                });
                applyGeneratedTitle(result.title, result.reply.created_at);
                updateSidebarAfterSend(`IVY: ${result.reply.content}`, result.reply.created_at);
            }
        } catch (error) {
            showChatNotice("Network error — your message was saved but IVY could not reply.");
        } finally {
            form.dataset.busy = "";
            setBusy(false);
            textarea.focus();
        }
    });

    textarea.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            form.requestSubmit();
        }
    });

    textarea.addEventListener("input", () => {
        textarea.style.height = "auto";
        textarea.style.height = `${Math.min(textarea.scrollHeight, 140)}px`;
    });

    // ── Per-chat menu actions ───────────────────────────────────────
    const renameModal = document.getElementById("renameAiModal");
    const renameTitleInput = document.getElementById("renameAiTitle");

    const openRenameModal = (chatId, currentTitle) => {
        pendingRenameTarget = chatId;
        if (renameTitleInput) renameTitleInput.value = currentTitle || "";
        if (renameModal) renameModal.hidden = false;
        if (renameTitleInput) {
            renameTitleInput.focus();
            renameTitleInput.select();
        }
    };

    document.querySelectorAll("[data-rename]").forEach((button) => {
        button.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            const menu = button.closest(".ai-chat-menu");
            if (menu) menu.removeAttribute("open");
            openRenameModal(button.dataset.rename, button.dataset.title);
        });
    });

    const renameForm = document.getElementById("renameAiForm");
    if (renameForm) {
        renameForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const newTitle = (renameTitleInput ? renameTitleInput.value : "").trim();
            if (!newTitle) return;

            try {
                const response = await fetch(`/ai/conversations/${pendingRenameTarget}/rename`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                    body: JSON.stringify({ title: newTitle }),
                });
                const result = await response.json().catch(() => ({}));
                if (!response.ok || !result.ok) {
                    showChatNotice(result.error || "Could not rename this chat.");
                    return;
                }

                const renamedItem = document.querySelector(
                    `.conversation-item[data-conversation-id="${pendingRenameTarget}"]`
                );
                if (renamedItem) {
                    renamedItem.dataset.conversationName = result.title.toLowerCase();
                    const nameEl = renamedItem.querySelector(".conversation-name-row strong");
                    if (nameEl) nameEl.textContent = result.title;
                    const time = renamedItem.querySelector(".conversation-name-row time");
                    if (time) {
                        time.dateTime = result.updated_at;
                        time.textContent = typeof formatChatTime === "function" ? formatChatTime(result.updated_at) : "";
                    }
                }
                if (String(pendingRenameTarget) === String(conversationId) && headerTitle) {
                    headerTitle.textContent = result.title;
                }
                const menuButton = renamedItem && renamedItem.querySelector("[data-rename]");
                if (menuButton) menuButton.dataset.title = result.title;
            } catch (error) {
                showChatNotice("Could not rename this chat.");
            } finally {
                if (renameModal) renameModal.hidden = true;
            }
        });
    }

    const deleteConversation = async (chatId, isActive) => {
        if (!window.confirm("Delete this AI conversation? Its messages will be removed permanently.")) return;
        try {
            const response = await fetch(`/ai/conversations/${chatId}/delete?ajax=1`, {
                method: "POST",
                headers: { "X-CSRFToken": getCsrfToken() },
            });
            const result = await response.json().catch(() => ({}));
            if (!response.ok || !result.ok) {
                showChatNotice("Could not delete this chat.");
                return;
            }
            if (isActive) {
                window.location.href = "/ai";
                return;
            }
            const item = document.querySelector(`.conversation-item[data-conversation-id="${chatId}"]`);
            if (item) item.remove();
        } catch (error) {
            showChatNotice("Could not delete this chat.");
        }
    };

    document.querySelectorAll("[data-delete]").forEach((button) => {
        button.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            const item = button.closest(".conversation-item");
            deleteConversation(button.dataset.delete, item && item.classList.contains("is-active"));
        });
    });

    const deleteChatBtn = document.getElementById("deleteChatBtn");
    if (deleteChatBtn) {
        deleteChatBtn.addEventListener("click", () => deleteConversation(conversationId, true));
    }

    // Close any open chat options menu when clicking elsewhere.
    document.addEventListener("click", (event) => {
        document.querySelectorAll(".ai-chat-menu[open]").forEach((menu) => {
            if (!menu.contains(event.target)) menu.removeAttribute("open");
        });
    });
})();

// ── AI Study Planner ────────────────────────────────────────────────────────
(function initStudyPlanner() {
    const modal = document.getElementById("studyPlannerModal");
    const button = document.getElementById("makePlanBtn");
    const openPlannerBtn = document.getElementById("openPlannerBtn");
    const emptyStateBtn = document.getElementById("emptyStatePlannerBtn");
    if (!modal || !button) return;

    if (openPlannerBtn) {
        openPlannerBtn.addEventListener("click", () => { modal.hidden = false; });
    }
    if (emptyStateBtn) {
        emptyStateBtn.addEventListener("click", () => { modal.hidden = false; });
    }
    modal.querySelectorAll("[data-close-modal]").forEach((closeBtn) => {
        closeBtn.addEventListener("click", () => { modal.hidden = true; });
    });
    modal.addEventListener("click", (event) => {
        if (event.target === modal) modal.hidden = true;
    });

    const getCsrfToken = () =>
        (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

    button.addEventListener("click", async () => {
        const subject = (document.getElementById("planSubject") || {}).value?.trim();
        const weeks = parseInt((document.getElementById("planWeeks") || {}).value, 10);
        const hours = parseInt((document.getElementById("planHours") || {}).value, 10);

        if (!subject) {
            alert("Please enter a subject for your study plan.");
            return;
        }

        button.disabled = true;
        button.textContent = "Generating plan…";

        try {
            const response = await fetch("/ai/study-plan", {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({ subject, weeks: weeks || 4, hours: hours || 2 }),
            });
            const result = await response.json().catch(() => ({}));
            if (!response.ok || !result.ok) {
                alert(result.error || "Could not generate a study plan right now.");
                return;
            }
            window.location.href = result.redirect || `/ai/${result.conversation_id}`;
        } catch (error) {
            alert("Network error — could not generate a study plan.");
        } finally {
            button.disabled = false;
            button.textContent = "Generate my study plan";
        }
    });
})();

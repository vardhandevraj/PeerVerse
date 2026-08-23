// IVY — shared theme and navigation interactions
(function initializeTheme() {
    const savedTheme = localStorage.getItem("ivy-theme") ||
        (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    document.documentElement.setAttribute("data-theme", savedTheme);
})();

function formatChatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function renderIvyMarkdown(element, text) {
    element.textContent = text || "";
    if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
        element.innerHTML = DOMPurify.sanitize(marked.parse(text || ""));
    }
}

function showChatNotice(message) {
    let notice = document.querySelector(".chat-client-notice");
    if (!notice) {
        notice = document.createElement("div");
        notice.className = "chat-client-notice";
        notice.setAttribute("role", "status");
        document.body.appendChild(notice);
    }
    notice.textContent = message;
    notice.classList.add("is-visible");
    window.clearTimeout(showChatNotice.timeoutId);
    showChatNotice.timeoutId = window.setTimeout(() => notice.classList.remove("is-visible"), 4200);
}

function createMessageElement(data, currentUserId) {
    const isIvy = data.message_type === "ai" || data.sender_id === null;
    const isOwn = String(data.sender_id) === String(currentUserId);
    const row = document.createElement("article");
    row.className = `message-row${isOwn ? " is-own" : ""}${isIvy ? " is-ivy" : ""}`;

    if (!isOwn) {
        const avatar = document.createElement("div");
        avatar.className = `message-avatar${isIvy ? " ivy-avatar" : ""}`;
        avatar.textContent = isIvy ? "✦" : (data.sender_name || "?").slice(0, 1).toUpperCase();
        row.appendChild(avatar);
    }

    const stack = document.createElement("div");
    stack.className = "message-stack";
    const senderLine = document.createElement("div");
    senderLine.className = "message-sender-line";
    const name = document.createElement("strong");
    name.textContent = isIvy ? "IVY" : (data.sender_name || "Peer");
    senderLine.appendChild(name);

    if (isIvy) {
        const badge = document.createElement("span");
        badge.className = "ivy-badge";
        badge.textContent = "AI learning assistant";
        senderLine.appendChild(badge);
    }

    const time = document.createElement("time");
    time.dateTime = data.created_at || "";
    time.textContent = formatChatTime(data.created_at);
    senderLine.appendChild(time);

    const bubble = document.createElement("div");
    bubble.className = `message-bubble${isIvy ? " ivy-message" : ""}`;
    const content = document.createElement("div");
    content.className = "message-content";
    if (isIvy) {
        content.dataset.markdown = "true";
        renderIvyMarkdown(content, data.message_text);
    } else {
        content.textContent = data.message_text || "";
    }
    bubble.appendChild(content);
    stack.append(senderLine, bubble);
    row.appendChild(stack);
    return row;
}

function updateConversationItem(data, currentConversationId) {
    const item = document.querySelector(`.conversation-item[data-conversation-id="${data.conversation_id}"]`);
    if (!item) return;

    const preview = item.querySelector(".conversation-preview");
    const unread = item.querySelector(".unread-count");
    const time = item.querySelector("time");
    const active = String(data.conversation_id) === String(currentConversationId);

    if (preview) {
        preview.textContent = `${data.message_type === "ai" ? "IVY: " : ""}${data.message_text || ""}`;
        preview.classList.toggle("has-unread", Boolean(data.is_unread && !active));
    }
    if (time) {
        time.dateTime = data.updated_at || "";
        time.textContent = formatChatTime(data.updated_at);
    }
    if (unread) {
        if (active || !data.is_unread) {
            unread.textContent = "";
            unread.classList.add("is-empty");
        } else {
            const current = Number.parseInt(unread.textContent, 10) || 0;
            unread.textContent = String(current + 1);
            unread.classList.remove("is-empty");
        }
    }
    item.parentElement.prepend(item);
}

function initializeChat(socket) {
    const form = document.getElementById("chatForm");
    const messageText = document.getElementById("messageText");
    const conversationInput = document.getElementById("conversationId");
    const userInput = document.getElementById("currentUserId");
    const chatBox = document.getElementById("chatBox");
    if (!form || !messageText || !conversationInput || !userInput || !chatBox || !socket) return;

    const conversationId = conversationInput.value;
    const currentUserId = userInput.value;

    const scrollToLatest = () => {
        chatBox.scrollTop = chatBox.scrollHeight;
    };
    const markCurrentConversationRead = () => socket.emit("mark_chat_read", { conversation_id: conversationId });
    const subscribe = () => {
        socket.emit("join_inbox");
        socket.emit("join_chat", { conversation_id: conversationId });
        markCurrentConversationRead();
    };

    socket.on("connect", subscribe);
    socket.on("receive_chat_message", (data) => {
        if (String(data.conversation_id) !== String(conversationId)) return;
        const welcome = chatBox.querySelector(".conversation-welcome");
        if (welcome) welcome.remove();
        chatBox.appendChild(createMessageElement(data, currentUserId));
        scrollToLatest();
        markCurrentConversationRead();
    });
    socket.on("conversation_updated", (data) => {
        updateConversationItem(data, conversationId);
        if (String(data.conversation_id) === String(conversationId)) markCurrentConversationRead();
    });
    socket.on("chat_error", (data) => showChatNotice(data.message || "Something went wrong with chat."));

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        const text = messageText.value.trim();
        if (!text) return;
        socket.emit("send_chat_message", { conversation_id: conversationId, message_text: text });
        messageText.value = "";
        messageText.style.height = "auto";
    });

    messageText.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            form.requestSubmit();
        }
    });
    messageText.addEventListener("input", () => {
        messageText.style.height = "auto";
        messageText.style.height = `${Math.min(messageText.scrollHeight, 140)}px`;
    });

    scrollToLatest();
}

// Video Learning Room invitations arrive on the personal inbox room no matter
// which page a student is browsing; joining happens on /call/<room_id>.
function initializeCallInvitations(socket) {
    if (!socket) return;
    let invitationCard = null;

    const dismiss = () => {
        if (invitationCard) {
            invitationCard.remove();
            invitationCard = null;
        }
    };

    socket.on("call_invitation", (data) => {
        if (!data || !data.room_id || !window.RTCPeerConnection) return;
        dismiss();

        invitationCard = document.createElement("div");
        invitationCard.className = "call-invitation-card";
        invitationCard.setAttribute("role", "alert");

        const copy = document.createElement("div");
        copy.className = "call-invitation-copy";
        const title = document.createElement("strong");
        title.textContent = `${(data.caller_name || "A classmate")} started a Learning Room call`;
        const sub = document.createElement("span");
        sub.textContent = "Join to learn together over video.";
        copy.append(title, sub);

        const actions = document.createElement("div");
        actions.className = "call-invitation-actions";
        const join = document.createElement("a");
        join.className = "call-invitation-join";
        join.href = `/call/${encodeURIComponent(data.room_id)}`;
        join.textContent = "Join";
        const ignore = document.createElement("button");
        ignore.type = "button";
        ignore.className = "call-invitation-dismiss";
        ignore.textContent = "Dismiss";
        ignore.addEventListener("click", dismiss);
        actions.append(join, ignore);

        invitationCard.append(copy, actions);
        document.body.appendChild(invitationCard);
        window.setTimeout(dismiss, 30000);
    });
}

document.addEventListener("DOMContentLoaded", () => {
    if (window.lucide) window.lucide.createIcons();

    const themeButton = document.getElementById("themeToggle");
    if (themeButton) {
        themeButton.addEventListener("click", () => {
            const current = document.documentElement.getAttribute("data-theme") || "dark";
            const next = current === "dark" ? "light" : "dark";
            document.documentElement.setAttribute("data-theme", next);
            localStorage.setItem("ivy-theme", next);
        });
    }

    const menuButton = document.getElementById("menuButton");
    const navLinks = document.getElementById("navLinks");
    if (menuButton && navLinks) menuButton.addEventListener("click", () => navLinks.classList.toggle("show"));

    document.querySelectorAll('.message-content[data-markdown="true"]').forEach((element) => {
        renderIvyMarkdown(element, element.textContent);
    });

    document.querySelectorAll("[data-open-modal]").forEach((button) => {
        button.addEventListener("click", () => {
            const modal = document.getElementById(button.dataset.openModal);
            if (modal) modal.hidden = false;
        });
    });
    document.querySelectorAll("[data-close-modal]").forEach((button) => {
        button.addEventListener("click", () => button.closest(".modal-backdrop").hidden = true);
    });
    // Call modals carry data-persistent so an active call cannot be dismissed by accident.
    document.querySelectorAll(".modal-backdrop:not([data-persistent])").forEach((backdrop) => {
        backdrop.addEventListener("click", (event) => {
            if (event.target === backdrop) backdrop.hidden = true;
        });
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") document.querySelectorAll(".modal-backdrop:not([data-persistent]):not([hidden])").forEach((modal) => modal.hidden = true);
    });

    const search = document.getElementById("conversationSearch");
    const noResults = document.getElementById("noSearchResults");
    if (search) {
        search.addEventListener("input", () => {
            const query = search.value.trim().toLowerCase();
            let matches = 0;
            document.querySelectorAll(".conversation-item").forEach((item) => {
                const visible = item.dataset.conversationName.includes(query);
                item.hidden = !visible;
                if (visible) matches += 1;
            });
            if (noResults) noResults.hidden = !query || matches > 0;
        });
    }

    document.querySelectorAll("[data-student-filter]").forEach((filter) => {
        const select = document.getElementById(filter.dataset.studentFilter);
        if (!select) return;
        filter.addEventListener("input", () => {
            const query = filter.value.trim().toLowerCase();
            Array.from(select.options).forEach((option) => {
                if (!option.value) return;
                option.hidden = !option.text.toLowerCase().includes(query);
            });
            const selectedOption = select.options[select.selectedIndex];
            if (selectedOption && selectedOption.hidden) select.value = "";
        });
    });

    // One shared socket carries chat, inbox updates, and Learning Room signaling.
    // Reuse a socket an earlier script (e.g. call_room.js) already opened.
    const socket = window.peerverseSocket || (window.io ? window.io() : null);
    if (socket) window.peerverseSocket = socket;
    if (socket) {
        window.peerverseSocket = socket;
        socket.on("connect", () => socket.emit("join_inbox"));
        initializeChat(socket);
        initializeCallInvitations(socket);
    }
});

// A subtle desktop-only cursor glow is shared across the application.
(function initializeAmbientGlow() {
    if (window.innerWidth < 800) return;
    const glow = document.createElement("div");
    glow.className = "ambient-cursor-glow";
    document.body.appendChild(glow);
    let mouseX = -500;
    let mouseY = -500;
    let currentX = -500;
    let currentY = -500;
    window.addEventListener("mousemove", (event) => {
        mouseX = event.clientX;
        mouseY = event.clientY;
        glow.style.opacity = "1";
    });
    window.addEventListener("mouseleave", () => { glow.style.opacity = "0"; });
    const render = () => {
        currentX += (mouseX - currentX) * 0.08;
        currentY += (mouseY - currentY) * 0.08;
        glow.style.left = `${currentX}px`;
        glow.style.top = `${currentY}px`;
        window.requestAnimationFrame(render);
    };
    render();
})();

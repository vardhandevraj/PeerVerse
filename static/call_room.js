// PeerVerse Video Learning Room.
// WebRTC RTCPeerConnection carries all audio/video between browsers;
// Flask-SocketIO carries only signaling, presence, and room chat.
(function initializeLearningRoom() {
    const roomInput = document.getElementById("roomId");
    if (!roomInput || !window.io || !window.RTCPeerConnection) return;

    const roomId = roomInput.value;
    const conversationId = document.getElementById("conversationId").value;
    const currentUserId = Number(document.getElementById("currentUserId").value);
    const currentUserLabel = document.getElementById("currentUserName").value;
    const leaveLink = document.getElementById("leaveLink");

    const videoGrid = document.getElementById("videoGrid");
    const stageBanner = document.getElementById("stageBanner");
    const participantCount = document.getElementById("participantCount");
    const permissionOverlay = document.getElementById("permissionOverlay");
    const permissionMessage = document.getElementById("permissionMessage");
    const retryMediaBtn = document.getElementById("retryMediaBtn");
    const joinAnywayBtn = document.getElementById("joinAnywayBtn");
    const chatPanel = document.getElementById("callChatPanel");
    const chatCloseBtn = document.getElementById("chatCloseBtn");
    const chatToggleBtn = document.getElementById("toggleChatBtn");
    const ivyThinking = document.getElementById("ivyThinking");
    const timeline = document.getElementById("chatTimeline");

    const micButton = document.getElementById("toggleMicBtn");
    const camButton = document.getElementById("toggleCamBtn");
    const screenButton = document.getElementById("toggleScreenBtn");
    const askIvyButton = document.getElementById("askIvyBtn");
    const endButton = document.getElementById("endCallBtn");

    // Fallback mirrors the server-side development default; real values come
    // from /api/call/ice which reads STUN_SERVER/TURN_* environment variables.
    let iceServers = [{ urls: ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"] }];

    const peers = new Map();          // user_id -> RTCPeerConnection (+ metadata)
    const pendingCandidates = new Map(); // user_id -> [RTCIceCandidate]
    const tiles = new Map();          // user_id -> {tile, video, fallback}
    const mediaState = new Map();     // user_id -> {mic, cam, screen}

    let localStream = null;
    let cameraTrack = null;
    let screenStream = null;
    let micEnabled = true;
    let camEnabled = true;
    let sharingScreen = false;
    let mediaReady = false;
    let joinedOnce = false;
    let callFinished = false;

    function showBanner(text, isError) {
        stageBanner.textContent = text;
        stageBanner.classList.toggle("is-error", Boolean(isError));
        stageBanner.hidden = false;
    }

    function hideBanner() { stageBanner.hidden = true; }

    function notice(text) {
        if (typeof showChatNotice === "function") showChatNotice(text);
    }

    // ── Tiles ────────────────────────────────────────────────────────
    function refreshGridLayout() {
        videoGrid.classList.toggle("is-solo", tiles.size <= 1);
        videoGrid.classList.toggle("is-pair", tiles.size === 2);
        videoGrid.classList.toggle("is-group", tiles.size > 2);
    }

    function ensureTile(userId, userName) {
        let tileEntry = tiles.get(userId);
        if (tileEntry) return tileEntry;

        const tile = document.createElement("div");
        tile.className = "video-tile";
        tile.dataset.userId = String(userId);

        const video = document.createElement("video");
        video.autoplay = true;
        video.playsInline = true;
        if (userId === currentUserId) video.muted = true;
        tile.appendChild(video);

        const fallback = document.createElement("div");
        fallback.className = "video-fallback";
        const avatar = document.createElement("span");
        avatar.className = "fallback-avatar";
        avatar.textContent = (userName || "?").slice(0, 1).toUpperCase();
        const nameHint = document.createElement("small");
        nameHint.textContent = userName || "Participant";
        fallback.append(avatar, nameHint);
        tile.appendChild(fallback);

        const label = document.createElement("span");
        label.className = "tile-label";
        tile.appendChild(label);

        if (userId === currentUserId) {
            const youBadge = document.createElement("span");
            youBadge.className = "tile-badge";
            youBadge.textContent = "You";
            label.appendChild(youBadge);
        }

        const nameSpan = document.createElement("span");
        nameSpan.textContent = userName || "Participant";
        label.appendChild(nameSpan);

        videoGrid.prepend(tile); // local tile stays visually first
        tileEntry = { tile, video, label, nameSpan };
        tiles.set(userId, tileEntry);
        refreshGridLayout();
        renderMediaBadges(userId);
        return tileEntry;
    }

    function dropTile(userId) {
        const entry = tiles.get(userId);
        if (!entry) return;
        entry.video.srcObject = null;
        entry.tile.remove();
        tiles.delete(userId);
        refreshGridLayout();
    }

    function setTileVideoActive(userId, hasVideo) {
        const entry = tiles.get(userId);
        if (!entry) return;
        entry.video.classList.toggle("is-hidden", !hasVideo);
        let fallback = entry.tile.querySelector(".video-fallback");
        if (!fallback) {
            fallback = document.createElement("div");
            fallback.className = "video-fallback";
            const avatar = document.createElement("span");
            avatar.className = "fallback-avatar";
            avatar.textContent = (entry.nameSpan.textContent || "?").slice(0, 1).toUpperCase();
            fallback.appendChild(avatar);
            entry.tile.insertBefore(fallback, entry.label);
        }
        fallback.style.display = hasVideo ? "none" : "flex";
    }

    function renderMediaBadges(userId) {
        const entry = tiles.get(userId);
        if (!entry) return;
        const state = mediaState.get(userId) || { mic: true, cam: true, screen: false };

        entry.label.querySelectorAll(".state-badge").forEach((badge) => badge.remove());
        if (state.screen) {
            const badge = document.createElement("span");
            badge.className = "tile-badge state-badge screen-badge";
            badge.textContent = "🖥";
            entry.label.appendChild(badge);
        }
        if (!state.mic || !state.cam) {
            const badge = document.createElement("span");
            badge.className = "tile-badge state-badge muted-dot";
            badge.textContent = state.cam ? "🎤" : "🚫";
            entry.label.appendChild(badge);
        }
        entry.tile.classList.toggle("is-screen-sharing", Boolean(state.screen));
    }

    function refreshParticipantCount() {
        participantCount.textContent = String(tiles.size);
    }

    // ── Signaling helpers ────────────────────────────────────────────
    function signal(remoteId, kind, extra) {
        socket.emit("call_signal", Object.assign({
            room_id: roomId,
            target_user_id: remoteId,
            kind,
        }, extra));
    }

    function flushCandidates(remoteId, pc) {
        const queued = pendingCandidates.get(remoteId);
        if (!queued) return;
        pendingCandidates.delete(remoteId);
        queued.forEach((candidate) => pc.addIceCandidate(candidate).catch(() => {}));
    }

    async function negotiate(pc, remoteId) {
        try {
            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);
            signal(remoteId, "offer", { type: offer.type, sdp: offer.sdp });
        } catch (error) {
            console.warn("Offer creation failed:", error);
        }
    }

    function attachRemoteTrack(userId, event) {
        const stream = event.streams[0];
        const entry = ensureTile(userId, peers.get(userId)?.remoteName || "Participant");
        entry.video.srcObject = stream;
        const hasVideo = (stream.getVideoTracks() || []).some((track) => track.readyState === "live");
        setTileVideoActive(userId, hasVideo);
        hideBanner();
    }

    // ── Peer connections ─────────────────────────────────────────────
    function ensurePeer(remoteId, remoteName) {
        if (remoteId === currentUserId) return null;
        let pc = peers.get(remoteId);
        if (pc) return pc;

        pc = new RTCPeerConnection({ iceServers });
        pc._remoteName = remoteName || "Participant";
        // Deterministic initiator prevents SDP glare: the lower id offers.
        pc._isInitiator = currentUserId < remoteId;

        if (localStream) {
            localStream.getTracks().forEach((track) => pc.addTrack(track, localStream));
        } else {
            // Pre-open sendrecv m-lines so participants who joined without
            // camera/microphone access can still receive media now and attach
            // their own tracks later (retry/screen share) via replaceTrack
            // without a fresh offer/answer round-trip.
            pc._audioTransceiver = pc.addTransceiver("audio", { direction: "sendrecv" });
            pc._videoTransceiver = pc.addTransceiver("video", { direction: "sendrecv" });
        }

        pc.onicecandidate = (event) => {
            if (event.candidate) signal(remoteId, "candidate", { candidate: event.candidate.toJSON() });
        };
        pc.ontrack = (event) => attachRemoteTrack(remoteId, event);
        pc.onnegotiationneeded = () => {
            if (pc._isInitiator && pc.signalingState === "stable" && !callFinished) negotiate(pc, remoteId);
        };
        pc.onconnectionstatechange = () => {
            if (callFinished) return;
            if (pc.connectionState === "failed") {
                if (pc._isInitiator && !pc._restarted) {
                    pc._restarted = true;
                    try { pc.restartIce(); } catch (_) { /* older browsers */ }
                } else if (!pc._restarted) {
                    pc._restarted = true;
                    // Give ICE a moment; a transient drop usually recovers itself.
                    window.setTimeout(() => {
                        if (peers.get(remoteId) === pc && ["failed", "closed"].includes(pc.connectionState)) {
                            dropPeer(remoteId);
                            showBanner(`Lost connection with ${pc._remoteName}. Reconnecting…`);
                        }
                    }, 2500);
                }
            } else if (pc.connectionState === "connected") {
                pc._restarted = false;
                hideBanner();
            }
        };

        peers.set(remoteId, pc);

        if (localStream && pc._isInitiator) {
            window.setTimeout(() => {
                if (peers.get(remoteId) === pc && pc.signalingState === "stable" && !callFinished) negotiate(pc, remoteId);
            }, 120); // slight stagger avoids simultaneous renegotiation bursts
        }
        return pc;
    }

    function closePeer(remoteId) {
        const pc = peers.get(remoteId);
        if (pc) {
            pc.ontrack = null;
            pc.onicecandidate = null;
            pc.close();
            peers.delete(remoteId);
        }
        pendingCandidates.delete(remoteId);
    }

    function dropPeer(remoteId, silent) {
        const name = peers.get(remoteId)?._remoteName;
        closePeer(remoteId);
        dropTile(remoteId);
        mediaState.delete(remoteId);
        refreshParticipantCount();
        if (!silent) notice(`${name || "A participant"} left the room.`);
    }

    function dropAllPeers() {
        Array.from(peers.keys()).forEach((id) => closePeer(id));
        Array.from(tiles.keys()).forEach((id) => { if (id !== currentUserId) dropTile(id); });
        mediaState.clear();
        refreshParticipantCount();
    }

    // ── Local media ──────────────────────────────────────────────────
    function mediaPermissionErrorText(error) {
        if (error && (error.name === "NotAllowedError" || error.name === "SecurityError")) {
            return "Permission was blocked. Allow camera and microphone access in your browser settings, or continue without media.";
        }
        if (error && error.name === "NotFoundError") {
            return "No camera or microphone was found. You can continue without media.";
        }
        return "Camera and microphone could not be started. You can continue without media.";
    }

    async function startLocalMedia() {
        permissionOverlay.hidden = true;
        try {
            localStream = await navigator.mediaDevices.getUserMedia({
                video: { width: { ideal: 1280 }, height: { ideal: 720 } },
                audio: { echoCancellation: true, noiseSuppression: true },
            });
            cameraTrack = localStream.getVideoTracks()[0] || null;
            mediaReady = true;
            micEnabled = true;
            camEnabled = true;
            micButton.disabled = false;
            camButton.disabled = false;
            micButton.classList.remove("is-off");
            camButton.classList.remove("is-off");
            const entry = ensureTile(currentUserId, currentUserLabel);
            entry.video.srcObject = localStream;
            setTileVideoActive(currentUserId, camEnabled && Boolean(cameraTrack));
            // Mid-call retries must reach peers that already exist.
            attachLocalTracksToPeers();
        } catch (error) {
            console.warn("getUserMedia failed:", error);
            localStream = null;
            cameraTrack = null;
            mediaReady = false;
            permissionMessage.textContent = mediaPermissionErrorText(error);
            permissionOverlay.hidden = false;
            ensureTile(currentUserId, currentUserLabel);
            setTileVideoActive(currentUserId, false);
        }
        refreshParticipantCount();
    }

    function continueWithoutMedia() {
        mediaReady = false;
        permissionOverlay.hidden = true;
        micEnabled = false;
        camEnabled = false;
        micButton.classList.add("is-off");
        camButton.classList.add("is-off");
        micButton.disabled = true;
        camButton.disabled = true;
        ensureTile(currentUserId, currentUserLabel);
        setTileVideoActive(currentUserId, false);
        refreshParticipantCount();
        hideBanner();
    }

    // ── Room lifecycle ───────────────────────────────────────────────
    function joinRoom() {
        if (callFinished) return;
        socket.emit("join_call", { room_id: roomId });
        broadcastMediaState();
    }

    function reconcileParticipants(participants) {
        dropAllPeers();
        (participants || []).forEach((person) => {
            const userId = Number(person.user_id);
            if (userId === currentUserId) return;
            ensurePeer(userId, person.user_name);
        });
        refreshParticipantCount();
    }

    function finishCall(message, redirectToConversation) {
        if (callFinished) return;
        callFinished = true;
        stopScreenShare(true);
        dropAllPeers();
        dropTile(currentUserId);
        if (localStream) {
            localStream.getTracks().forEach((track) => track.stop());
            localStream = null;
        }
        showBanner(message || "The call has ended.", false);
        endButton.disabled = true;
        if (redirectToConversation !== false && conversationId) {
            window.setTimeout(() => {
                window.location.href = `/messages/${conversationId}`;
            }, 1600);
        }
    }

    // ── Media controls ───────────────────────────────────────────────
    function broadcastMediaState() {
        socket.emit("call_media_state", {
            room_id: roomId,
            media: { mic: micEnabled, cam: camEnabled, screen: sharingScreen },
        });
    }

    micButton.addEventListener("click", () => {
        const track = localStream && localStream.getAudioTracks()[0];
        if (!track) return;
        micEnabled = !micEnabled;
        track.enabled = micEnabled;
        micButton.classList.toggle("is-off", !micEnabled);
        setTileVideoActive(currentUserId, camEnabled && Boolean(cameraTrack));
        broadcastMediaState();
    });

    camButton.addEventListener("click", () => {
        if (sharingScreen) {
            camEnabled = !camEnabled; // remembered for when the share stops
            camButton.classList.toggle("is-off", !camEnabled);
            broadcastMediaState();
            return;
        }
        const track = cameraTrack;
        if (!track) return;
        camEnabled = !camEnabled;
        track.enabled = camEnabled;
        camButton.classList.toggle("is-off", !camEnabled);
        setTileVideoActive(currentUserId, camEnabled);
        broadcastMediaState();
    });

    async function startScreenShare() {
        if (!navigator.mediaDevices.getDisplayMedia) {
            notice("Screen sharing is not supported in this browser.");
            return;
        }
        try {
            screenStream = await navigator.mediaDevices.getDisplayMedia({
                video: { frameRate: { ideal: 15 } },
                audio: false,
            });
            const screenTrack = screenStream.getVideoTracks()[0];
            sharingScreen = true;
            screenButton.classList.add("is-off");
            replaceOutgoingVideoTrack(screenTrack);
            screenTrack.addEventListener("ended", () => stopScreenShare());
            const entry = tiles.get(currentUserId);
            if (entry && entry.video.srcObject) {
                // Show the shared screen in your own tile too.
                const composite = new MediaStream([screenTrack]);
                entry.video.srcObject = composite;
                setTileVideoActive(currentUserId, true);
            }
            broadcastMediaState();
        } catch (error) {
            if (error && error.name !== "NotAllowedError") console.warn("Screen share failed:", error);
        }
    }

    function stopScreenShare(silent) {
        if (!screenStream) return;
        const screenTrack = screenStream.getVideoTracks()[0];
        screenStream.getTracks().forEach((track) => track.stop());
        screenStream = null;
        sharingScreen = false;
        screenButton.classList.remove("is-off");
        if (cameraTrack) {
            cameraTrack.enabled = camEnabled;
            replaceOutgoingVideoTrack(cameraTrack);
        } else {
            // No camera to restore: actively detach the screen track so
            // remote participants do not keep a frozen frame.
            replaceOutgoingVideoTrack(null);
        }
        const entry = tiles.get(currentUserId);
        if (entry) entry.video.srcObject = localStream;
        setTileVideoActive(currentUserId, camEnabled && Boolean(cameraTrack));
        if (!silent) broadcastMediaState();
    }

    function outgoingVideoSender(pc) {
        const active = pc.getSenders().find((item) => item.track && item.track.kind === "video");
        if (active) return active;
        if (pc._videoTransceiver) return pc._videoTransceiver.sender;
        return pc.getSenders().find((item) => !item.track) || null;
    }

    function replaceOutgoingVideoTrack(newTrack) {
        peers.forEach((pc) => {
            const sender = outgoingVideoSender(pc);
            if (sender && sender.track !== newTrack) sender.replaceTrack(newTrack).catch(() => {});
        });
    }

    function attachLocalTracksToPeers() {
        if (!localStream) return;
        localStream.getTracks().forEach((track) => {
            peers.forEach((pc) => {
                let sender = pc.getSenders().find((item) => item.track && item.track.kind === track.kind);
                if (!sender && track.kind === "video" && pc._videoTransceiver) sender = pc._videoTransceiver.sender;
                if (!sender && track.kind === "audio" && pc._audioTransceiver) sender = pc._audioTransceiver.sender;
                if (sender && sender.track !== track) sender.replaceTrack(track).catch(() => {});
            });
        });
    }

    screenButton.addEventListener("click", () => (sharingScreen ? stopScreenShare() : startScreenShare()));

    // ── Chat ─────────────────────────────────────────────────────────
    const chatForm = document.getElementById("chatForm");
    const messageText = document.getElementById("messageText");

    function appendMessage(data) {
        const welcome = timeline.querySelector(".conversation-welcome");
        if (welcome) welcome.remove();
        timeline.appendChild(createMessageElement(data, String(currentUserId)));
        timeline.scrollTop = timeline.scrollHeight;
    }

    function removeIvyThinking() {
        ivyThinking.hidden = true;
        timeline.querySelectorAll(".ivy-thinking-note").forEach((note) => note.remove());
    }

    chatForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const text = messageText.value.trim();
        if (!text) return;
        socket.emit("send_call_message", { room_id: roomId, message_text: text });
        messageText.value = "";
        messageText.style.height = "auto";
    });

    messageText.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            chatForm.requestSubmit();
        }
    });
    messageText.addEventListener("input", () => {
        messageText.style.height = "auto";
        messageText.style.height = `${Math.min(messageText.scrollHeight, 140)}px`;
    });

    chatToggleBtn.addEventListener("click", () => {
        chatPanel.classList.add("is-open");
        messageText.focus({ preventScroll: true });
    });
    chatCloseBtn.addEventListener("click", () => chatPanel.classList.remove("is-open"));

    askIvyButton.addEventListener("click", () => {
        chatPanel.classList.add("is-open");
        if (!messageText.value.trim()) messageText.value = "@ivy ";
        messageText.focus({ preventScroll: true });
        messageText.setSelectionRange(messageText.value.length, messageText.value.length);
    });

    endButton.addEventListener("click", () => {
        socket.emit("end_call", { room_id: roomId });
    });

    function leaveWithoutEnding(event) {
        if (callFinished) return; // navigation handled by finishCall
        event.preventDefault();
        callFinished = true;
        socket.emit("leave_call", { room_id: roomId });
        if (localStream) localStream.getTracks().forEach((track) => track.stop());
        window.setTimeout(() => { window.location.href = leaveLink.href; }, 180);
    }
    leaveLink.addEventListener("click", leaveWithoutEnding);
    window.addEventListener("pagehide", () => {
        if (!callFinished) socket.emit("leave_call", { room_id: roomId });
    });

    retryMediaBtn.addEventListener("click", () => startLocalMedia().then(broadcastMediaState)); // startLocalMedia attaches tracks itself
    joinAnywayBtn.addEventListener("click", continueWithoutMedia);

    // ── Socket wiring ────────────────────────────────────────────────
    // One shared app socket: reuse the one script.js created, or create it
    // here first so script.js reuses ours instead of opening a second
    // connection (call_room.js runs before script.js's DOMContentLoaded).
    const socket = window.peerverseSocket || window.io();
    window.peerverseSocket = socket;

    socket.on("connect", () => {
        if (joinedOnce) {
            // Reconnection path: rebuild presence and every peer connection.
            showBanner("Reconnected — restoring the room…");
            dropAllPeers();
            joinRoom();
        }
    });

    socket.on("disconnect", () => {
        if (!callFinished) showBanner("Connection lost — reconnecting…", true);
    });

    socket.on("call_joined", (data) => {
        if (!data || data.room_id !== roomId) return;
        joinedOnce = true;
        hideBanner();
        reconcileParticipants(data.participants);
    });

    socket.on("call_peer_joined", (data) => {
        if (!data || data.room_id !== roomId || Number(data.user_id) === currentUserId) return;
        ensurePeer(Number(data.user_id), data.user_name);
        refreshParticipantCount();
        notice(`${data.user_name} joined the room.`);
    });

    socket.on("call_peer_left", (data) => {
        if (!data || data.room_id !== roomId) return;
        const name = peers.get(Number(data.user_id))?._remoteName || "Your study partner";
        dropPeer(Number(data.user_id), true);
        notice(`${name} left the room.`);
    });

    socket.on("call_media_state", (data) => {
        if (!data || data.room_id !== roomId) return;
        mediaState.set(Number(data.user_id), data.media);
        renderMediaBadges(Number(data.user_id));
    });

    socket.on("call_signal", async (data) => {
        if (!data || data.room_id !== roomId || Number(data.sender_id) === currentUserId) return;
        const remoteId = Number(data.sender_id);

        if (data.kind === "offer" && data.description) {
            const pc = ensurePeer(remoteId, data.sender_name);
            try {
                await pc.setRemoteDescription(new RTCSessionDescription(data.description));
                flushCandidates(remoteId, pc);
                const answer = await pc.createAnswer();
                await pc.setLocalDescription(answer);
                signal(remoteId, "answer", { type: answer.type, sdp: answer.sdp });
            } catch (error) {
                console.warn("Failed handling offer:", error);
            }
        } else if (data.kind === "answer" && data.description) {
            const pc = peers.get(remoteId);
            if (pc && pc.signalingState === "have-local-offer") {
                try {
                    await pc.setRemoteDescription(new RTCSessionDescription(data.description));
                    flushCandidates(remoteId, pc);
                    hideBanner();
                } catch (error) {
                    console.warn("Failed applying answer:", error);
                }
            }
        } else if (data.kind === "candidate" && data.candidate) {
            const pc = peers.get(remoteId);
            const candidate = new RTCIceCandidate(data.candidate);
            if (pc && pc.remoteDescription) {
                pc.addIceCandidate(candidate).catch(() => {});
            } else if (pc) {
                if (!pendingCandidates.has(remoteId)) pendingCandidates.set(remoteId, []);
                pendingCandidates.get(remoteId).push(candidate);
            }
        }
    });

    socket.on("receive_call_message", (data) => {
        if (!data || data.room_id !== roomId) return;
        // Only an actual IVY reply ends the "thinking" state; normal chat
        // messages arriving meanwhile must not clear it.
        if (data.message_type === "ai" || data.sender_id === null) removeIvyThinking();
        appendMessage(data);
    });

    socket.on("ivy_thinking", (data) => {
        if (!data || data.room_id !== roomId) return;
        if (ivyThinking.hidden) {
            ivyThinking.hidden = false;
            const note = document.createElement("div");
            note.className = "ivy-thinking-note";
            note.textContent = "✦ IVY is preparing an answer…";
            timeline.appendChild(note);
            timeline.scrollTop = timeline.scrollHeight;
        }
    });

    socket.on("call_error", (data) => {
        const message = (data && data.message) || "Something went wrong with this call.";
        showBanner(message, true);
        notice(message);
        if (/ended|no longer exists|not a participant|access/i.test(message)) {
            finishCall(message, false);
            window.setTimeout(() => { window.location.href = "/messages"; }, 2200);
        }
    });

    socket.on("call_ended", (data) => {
        if (!data || data.room_id !== roomId) return;
        removeIvyThinking();
        finishCall("The call has ended. Returning to your conversation…");
    });

    // ── Boot sequence ────────────────────────────────────────────────
    fetch("/api/call/ice", { credentials: "same-origin" })
        .then((response) => (response.ok ? response.json() : null))
        .then((config) => {
            if (config && Array.isArray(config.ice_servers) && config.ice_servers.length) {
                iceServers = config.ice_servers;
            }
        })
        .catch(() => {})
        .finally(() => {
            startLocalMedia().then(() => {
                joinRoom();
                refreshParticipantCount();
            });
        });
})();

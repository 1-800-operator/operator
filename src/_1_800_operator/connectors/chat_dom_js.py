"""
JS strings for the Google Meet chat-panel DOM scraping.

`attach_adapter` injects these into the meeting page: a MutationObserver
on the chat panel, a JS-side queue drained from Python, a snapshot of
message IDs for send-readback, and a walk of participant tiles. Kept in
this module so DOM quirks land in one place if a future bridge ever
needs them too.
"""


# Snapshot all current chat message IDs. Used by send_chat to capture a
# pre-send baseline so we can detect which new ID corresponds to our
# own send (poll for set difference). Returns a list of strings.
SNAPSHOT_MESSAGE_IDS_JS = (
    "() => Array.from(document.querySelectorAll('div[data-message-id]'))"
    ".map(el => el.getAttribute('data-message-id'))"
)


# Install a MutationObserver over the chat panel that pushes new messages
# into window.__operatorChatQueue (which the Python side drains via
# DRAIN_CHAT_QUEUE_JS). Idempotent — checks for an existing observer
# and returns early if one is already attached. Returns nothing; caller
# must verify attachment with OBSERVER_ATTACHED_CHECK_JS afterwards
# because the function silently no-ops if the chat textarea isn't in
# the DOM yet.
INSTALL_CHAT_OBSERVER_JS = """() => {
    if (window.__operatorChatObserver) return;
    window.__operatorChatQueue = [];
    window.__operatorSeenIds = new Set();

    // Seed seen IDs with all existing messages so we don't re-process history
    document.querySelectorAll('div[data-message-id]').forEach(el => {
        window.__operatorSeenIds.add(el.getAttribute('data-message-id'));
    });

    function extractMessage(el) {
        const msgId = el.getAttribute('data-message-id');
        if (!msgId || window.__operatorSeenIds.has(msgId)) return null;
        window.__operatorSeenIds.add(msgId);
        // Extract text — prefer first div[jsname] inside message (any jsname value),
        // fall back to first child's first text node, then raw innerText.
        const jsnameEl = el.querySelector('div[jsname]');
        let text = '';
        if (jsnameEl) {
            text = jsnameEl.innerText.trim();
        } else if (el.children[0]) {
            const fc = el.children[0].childNodes[0];
            text = (fc && fc.textContent) ? fc.textContent.trim() : el.innerText.trim();
        } else {
            text = el.innerText.trim();
        }
        // Extract sender — walk up to 4 parents, find a sibling div whose
        // FIRST child has its OWN sibling that's the timestamp. The sender
        // header is rendered as two stacked leaves: [name leaf] + [timestamp
        // leaf]. We pick the parent whose first text-bearing leaf differs
        // from the message body's leaf, which is the structural sender
        // header. Locale-agnostic — prior implementation used an AM/PM
        // regex on the rendered timestamp which broke on 24-hour locales
        // (de-DE, fr-FR, most of EU/Asia: "14:30" with no AM/PM marker).
        let sender = '';
        let foundSender = false;
        let node = el;
        for (let d = 0; d < 4 && !foundSender; d++) {
            node = node.parentElement;
            if (!node) break;
            for (const sib of node.children) {
                if (sib === el || sib.contains(el)) continue;
                const t = (sib.innerText || '').trim();
                if (!t) continue;
                const lines = t.split('\\n').map(s => s.trim()).filter(Boolean);
                // The header always renders as 2 stacked leaves: name then
                // timestamp. A 2-line block with the SECOND line containing
                // digits is the sender header.
                if (lines.length >= 2 && /\\d/.test(lines[1])) {
                    sender = lines[0];
                    foundSender = true;
                    break;
                }
            }
        }
        return {id: msgId, sender: sender, text: text, t_dom: Date.now()};
    }

    // Locate the chat panel directly by structural signal — the one
    // [data-panel-id] container that EITHER contains the chat textarea
    // OR has [data-message-id] descendants. Two positive signals OR'd
    // together so we survive the empty-chat case AND the textarea-
    // rearranged-elsewhere case. Prior implementation used closest()
    // from the textarea, which would have silently picked the wrong
    // panel if Meet ever moved the textarea outside its panel.
    let container = null;
    for (const panel of document.querySelectorAll('[data-panel-id]')) {
        if (panel.querySelector('textarea[aria-label="Send a message"]') ||
            panel.querySelector('[data-message-id]')) {
            container = panel;
            break;
        }
    }
    if (!container) return;

    window.__operatorChatObserver = new MutationObserver(mutations => {
        for (const mut of mutations) {
            for (const node of mut.addedNodes) {
                if (node.nodeType !== 1) continue;
                // Check if the added node itself is a message
                if (node.matches && node.matches('div[data-message-id]')) {
                    const msg = extractMessage(node);
                    if (msg) window.__operatorChatQueue.push(msg);
                }
                // Check descendants
                if (node.querySelectorAll) {
                    node.querySelectorAll('div[data-message-id]').forEach(el => {
                        const msg = extractMessage(el);
                        if (msg) window.__operatorChatQueue.push(msg);
                    });
                }
            }
        }
    });
    window.__operatorChatObserver.observe(container, {childList: true, subtree: true});
}"""


# Returns true iff the observer attached. The install function returns
# early (no-op) if the chat textarea isn't in the DOM yet — page.evaluate
# won't throw on the no-op, so we check this explicitly to know whether
# to retry on the next poll.
OBSERVER_ATTACHED_CHECK_JS = "() => !!window.__operatorChatObserver"


# --- Google Chat iframe SEND path (S250) ------------------------------------
# Sending into the space embed: focus + clear the contenteditable, then the
# Python side types the text via CDP Input.insertText (the real input
# pipeline — execCommand insertText puts text in visually but Google's editor
# leaves the Send button DISABLED because its synthetic event isn't
# registered; Input.insertText enables it). Then click Send. Validated live
# S250. The input is a div[contenteditable="true"][role="textbox"]; the Send
# button is button[aria-label="Send message"], disabled until the editor sees
# content.

# Focus + clear the editable, insert `msg`, and notify the editor. Returns
# true if the editable was found.
#
# Insertion uses execCommand('insertText') — which preserves astral-plane
# characters like the [🤖 Claude] prefix that CDP Input.insertText drops when
# they're embedded mid-string — followed by an explicit InputEvent dispatch.
# Google's model-based editor leaves the Send button DISABLED on a bare
# execCommand (it doesn't sync from raw DOM mutations); the dispatched
# InputEvent is what makes it register the content and enable Send. Both
# verified live S250. execCommand is deprecated but functional in Chrome 148;
# if it ever stops, fall back to per-call CDP Input.insertText (drops embedded
# emoji) or a paste-based path.
GCHAT_INSERT_JS = """(msg) => {
    const ed = document.querySelector('div[contenteditable="true"][role="textbox"]')
            || document.querySelector('div[contenteditable="true"]');
    if (!ed) return false;
    ed.focus();
    document.execCommand('selectAll', false, null);
    document.execCommand('delete', false, null);
    document.execCommand('insertText', false, msg);
    ed.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: msg}));
    return true;
}"""

# Click Send if it's present + enabled. Returns true if clicked, false if the
# button is missing or still disabled (caller polls — it enables once the
# editor registers the inserted text).
GCHAT_CLICK_SEND_JS = """() => {
    const b = document.querySelector('button[aria-label="Send message"]');
    if (!b || b.disabled) return false;
    b.click();
    return true;
}"""


# Drain window.__operatorChatQueue and reset it to an empty array.
# Returns the drained list of {id, sender, text} message dicts. Safe
# to call before the observer attaches — returns [] in that case.
DRAIN_CHAT_QUEUE_JS = """() => {
    const q = window.__operatorChatQueue || [];
    window.__operatorChatQueue = [];
    return q;
}"""


# ---------------------------------------------------------------------------
# Google Chat space-embed variant (S250)
# ---------------------------------------------------------------------------
# When a Meet is attached to a Google Chat space, Meet renders chat inside a
# cross-origin chat.google.com iframe instead of the in-page [data-panel-id]
# panel. The classic observer above queries the parent document and can't
# reach it (same-origin policy). This observer is installed INTO that frame
# via Playwright's Frame.evaluate — the frame has its own window, so it
# reuses the same __operatorChat* globals and the same DRAIN_CHAT_QUEUE_JS /
# OBSERVER_ATTACHED_CHECK_JS work unchanged against the frame.
#
# Message identity differs from classic Meet chat:
#   - container : c-wiz[data-topic-id][data-is-user-topic="true"]
#                 (the [data-is-user-topic] guard skips the
#                 "firstHistoryFurniture" system node + reaction-button
#                 nodes, which also carry data-topic-id)
#   - id        : data-topic-id (e.g. "MFivfrcBGcI" — NOT a "spaces/..." id;
#                 the read_chat spaces/ filter must be bypassed for these)
#   - sender    : descendant [data-message-id][role="heading"] innerText
#                 ("You" for the dial profile's own posts)
#   - body      : [jsname="bgckF"] innerText
#   - t_dom     : data-local-sort-time-msec (epoch ms — already the same
#                 clock as classic's Date.now())
# Selectors validated live against a space-attached meeting (S250,
# debug probes via CDP). jsname values (bgckF) rotate ~quarterly like Meet's
# obfuscated classes; the data-* attrs are stable. Body falls back to
# innerText-minus-heading if the jsname rotates so a rotation degrades to a
# slightly noisier body rather than zero capture.
INSTALL_GCHAT_OBSERVER_JS = """() => {
    if (window.__operatorChatObserver) return;
    window.__operatorChatQueue = [];
    window.__operatorSeenIds = new Set();

    function extractMessage(topic) {
        const id = topic.getAttribute('data-topic-id');
        if (!id || window.__operatorSeenIds.has(id)) return null;
        window.__operatorSeenIds.add(id);
        let sender = '';
        const h = topic.querySelector('[data-message-id][role="heading"]');
        if (h) {
            // The heading's innerText concatenates the name with workspace
            // badges ("…, domain_disabledExternal user not managed by admin").
            // span.njhDLd is the clean name leaf; fall back to the full
            // heading text if that obfuscated class rotates.
            const nameEl = h.querySelector('span.njhDLd');
            sender = ((nameEl ? nameEl.innerText : h.innerText) || '').trim();
        }
        let text = '';
        const bodyEl = topic.querySelector('[jsname="bgckF"]');
        if (bodyEl) {
            text = (bodyEl.innerText || '').trim();
        } else {
            // jsname rotated — fall back to topic text minus the heading leaf.
            const full = (topic.innerText || '').trim();
            text = h ? full.split(h.innerText).join(' ').replace(/\\s+/g, ' ').trim() : full;
        }
        const t = topic.getAttribute('data-local-sort-time-msec');
        return {id: id, sender: sender, text: text,
                t_dom: t ? parseInt(t, 10) : Date.now()};
    }

    // Seed seen IDs with existing messages so we don't re-emit history.
    document.querySelectorAll('[data-topic-id][data-is-user-topic="true"]')
        .forEach(t => window.__operatorSeenIds.add(t.getAttribute('data-topic-id')));

    // Observe the message-list region. role=main is the chat scroller; fall
    // back to body so a role rename still attaches (subtree:true catches the
    // topic nodes either way).
    const root = document.querySelector('[role="main"]') || document.body;
    if (!root) return;

    window.__operatorChatObserver = new MutationObserver(mutations => {
        for (const mut of mutations) {
            for (const node of mut.addedNodes) {
                if (node.nodeType !== 1) continue;
                if (node.matches &&
                    node.matches('[data-topic-id][data-is-user-topic="true"]')) {
                    const msg = extractMessage(node);
                    if (msg) window.__operatorChatQueue.push(msg);
                }
                if (node.querySelectorAll) {
                    node.querySelectorAll('[data-topic-id][data-is-user-topic="true"]')
                        .forEach(el => {
                            const msg = extractMessage(el);
                            if (msg) window.__operatorChatQueue.push(msg);
                        });
                }
            }
        }
    });
    window.__operatorChatObserver.observe(root, {childList: true, subtree: true});
}"""


# Iframe-specific drain. Same contract as DRAIN_CHAT_QUEUE_JS (returns +
# clears the queue) but re-resolves the sender from the live DOM for any
# queued message whose sender is empty or a "Name loading…" placeholder.
#
# Two cases it heals, both at drain time over the full live DOM:
#  1. Name-loading race — the MutationObserver fires the instant Google Chat
#     mounts a message node, sometimes a beat before the author's display name
#     resolves. The text is stable immediately; only the name lags. By drain
#     (next poll, ~500ms later) it has resolved, so we re-read by data-topic-id.
#  2. Grouped continuation — Google Chat omits the heading on consecutive
#     same-author messages, so those topics carry no name of their own. Every
#     topic does carry data-creator-id, so we resolve the name from any
#     rendered sibling with the SAME creator-id that does have a heading. This
#     is robust to the Python-side history cap: resolution happens in the DOM
#     before the message enters any capped buffer, and the group's head message
#     (which has the name) is always co-rendered in the live DOM.
# CSS.escape guards against non-identifier chars in the id / creator-id.
DRAIN_GCHAT_QUEUE_JS = """() => {
    const q = window.__operatorChatQueue || [];
    window.__operatorChatQueue = [];
    const PLACEHOLDER = /^(name loading|loading)/i;
    const nameOf = (topic) => {
        const h = topic.querySelector('[data-message-id][role="heading"]');
        if (!h) return '';
        const nameEl = h.querySelector('span.njhDLd');
        const n = ((nameEl ? nameEl.innerText : h.innerText) || '').trim();
        return PLACEHOLDER.test(n) ? '' : n;
    };
    for (const m of q) {
        if (m.sender && !PLACEHOLDER.test(m.sender)) continue;
        let topic = null;
        try {
            topic = document.querySelector('[data-topic-id="' + CSS.escape(m.id) + '"]');
        } catch (e) { topic = null; }
        if (!topic) continue;
        // 1. The message's own heading (resolves the Name-loading race).
        let name = nameOf(topic);
        // 2. Grouped continuation: resolve by data-creator-id from a sibling.
        if (!name) {
            const cid = topic.getAttribute('data-creator-id');
            if (cid) {
                try {
                    const sibs = document.querySelectorAll(
                        '[data-topic-id][data-is-user-topic="true"][data-creator-id="'
                        + CSS.escape(cid) + '"]');
                    for (const s of sibs) {
                        const n = nameOf(s);
                        if (n) { name = n; break; }
                    }
                } catch (e) {}
            }
        }
        if (name) m.sender = name;
    }
    return q;
}"""


# Participant name scrape via tile DOM. Meet renders one tile per
# participant with `data-requested-participant-id`; the display name
# lives in a `span.notranslate` leaf inside each tile. Returns a
# deduped list; callers must degrade gracefully on [].
GET_PARTICIPANT_NAMES_JS = """() => {
    const tiles = document.querySelectorAll('[data-requested-participant-id]');
    const names = [];
    const seen = new Set();
    tiles.forEach(function(tile) {
        const span = tile.querySelector('span.notranslate');
        const name = span ? (span.textContent || '').trim() : '';
        if (name && !seen.has(name)) {
            seen.add(name);
            names.push(name);
        }
    });
    return names;
}"""


# Return the operator-runner's Meet display name from the dial Chrome.
# Identifies the LOCAL tile by the presence of CAMERA CONTROLS — specifically
# the "Reframe" or "Backgrounds and effects" buttons. These only render on
# your own tile because they only apply to your own video stream. Validated
# in 2-, 3-, and 4-person meetings:
#   local tile  → has "Reframe" + "Backgrounds and effects" + "More options"
#   remote tile → has "Pin", "You can't unmute someone else", "More options"
#
# Prior predicate "no 'Pin <name>' button" silently mis-identified in
# 2-person calls because Meet hides the Pin button entirely in 1-on-1s (no
# use case for pinning when there's only one other person), so both tiles
# matched and the picker returned the first in DOM order — typically the
# remote. The camera-controls predicate works for both 1-on-1 (where remote
# tiles render no buttons at all) and 3+ person (where remote tiles render
# Pin/unmute/More-options but never camera controls).
#
# Localization caveat: "Reframe" and "Backgrounds and effects" are English
# aria-labels; non-English locales would silently break. Today the dial
# Chrome's UI language is whatever Meet renders for that Google account.
#
# Returns "" on failure — caller falls back to a generic label.
GET_SELF_NAME_JS = """() => {
    const tiles = document.querySelectorAll('[data-requested-participant-id]');
    for (const tile of tiles) {
        if (!tile.querySelector('button[aria-label="Reframe"], button[aria-label="Backgrounds and effects"]')) continue;
        const span = tile.querySelector('span.notranslate');
        if (span) {
            const name = (span.textContent || '').trim();
            if (name) return name;
        }
    }
    return '';
}"""


# Install a MutationObserver on every REMOTE participant tile that fires
# when the speaking-indicator class (BlxGDf) appears or disappears on any
# descendant element. The local tile is identified by the same predicate
# as GET_SELF_NAME_JS (presence of "Reframe" / "Backgrounds and effects"
# camera-control buttons) and deliberately skipped — dial Chrome's
# system-audio output never contains the runner's own voice (Meet doesn't
# echo your mic back to your speakers), so a "speaking" local tile would
# be mic activity and misattribute remote [S] audio to the runner.
#
# Idempotent at the per-tile level: re-running the install attaches
# observers to NEW tiles (late joiners) without disconnecting existing
# observers or clearing the speaking queue. Callers can — and should —
# invoke this periodically as a rescan. Observers, state, and queue are
# preserved across calls.
#
# Pushes {participant_id, name, speaking, t} events to
# window.__operatorSpeakingQueue; Python drains via DRAIN_SPEAKING_QUEUE_JS.
# Returns {total_observed, added, added_names, local_pid}:
#   total_observed — current size of the observer dict
#   added          — new tiles wired up on this call (0 on a no-op rescan)
#   added_names    — display names of tiles added on this call
#   local_pid      — skipped local tile's participant id (or "")
INSTALL_SPEAKING_OBSERVER_JS = """() => {
    if (!window.__operatorSpeakingObservers) {
        window.__operatorSpeakingObservers = {};
        window.__operatorSpeakingState = {};
        window.__operatorSpeakingQueue = [];
    }

    function getName(tile) {
        var span = tile.querySelector('span.notranslate');
        return span ? (span.textContent || '').trim() : '';
    }

    function hasSpeakingClass(tile) {
        var els = tile.querySelectorAll('*');
        for (var i = 0; i < els.length; i++) {
            if (els[i].classList.contains('BlxGDf')) return true;
        }
        return false;
    }

    function speakingClassCount(tile) {
        var els = tile.querySelectorAll('*');
        var n = 0;
        for (var i = 0; i < els.length; i++) {
            if (els[i].classList.contains('BlxGDf')) n += 1;
        }
        return n;
    }

    function isLocalTile(tile) {
        return !!tile.querySelector('button[aria-label="Reframe"], button[aria-label="Backgrounds and effects"]');
    }

    // Capture per-tile DOM state at the exact instant a speaker observer
    // fires. Used for forensic attribution debugging — correlate against a
    // screen recording to see whether Meet's BlxGDf class was on the
    // expected tile when the observer triggered. Cheap (only walks tiles,
    // not full DOM) and always built, so Python can gate the on-disk dump
    // without re-evaluating JS.
    function snapshotAllTiles() {
        var out = [];
        var allTiles = document.querySelectorAll('[data-requested-participant-id]');
        for (var i = 0; i < allTiles.length; i++) {
            var t = allTiles[i];
            out.push({
                pid: t.getAttribute('data-requested-participant-id') || '',
                name: getName(t),
                has_speaking_class: hasSpeakingClass(t),
                speaking_class_count: speakingClassCount(t),
                is_local: isLocalTile(t),
            });
        }
        return out;
    }

    var tiles = document.querySelectorAll('[data-requested-participant-id]');

    var localPid = '';
    for (var i = 0; i < tiles.length; i++) {
        if (!isLocalTile(tiles[i])) continue;
        var sp = tiles[i].querySelector('span.notranslate');
        if (sp && (sp.textContent || '').trim()) {
            localPid = tiles[i].getAttribute('data-requested-participant-id') || '';
            break;
        }
    }

    var added = 0;
    var addedNames = [];

    tiles.forEach(function(tile) {
        var id = tile.getAttribute('data-requested-participant-id');
        if (!id || id === localPid) return;
        if (window.__operatorSpeakingObservers[id]) return;

        window.__operatorSpeakingState[id] = hasSpeakingClass(tile);
        added += 1;
        addedNames.push(getName(tile));

        var obs = new MutationObserver(function() {
            var now = hasSpeakingClass(tile);
            if (now !== window.__operatorSpeakingState[id]) {
                window.__operatorSpeakingState[id] = now;
                window.__operatorSpeakingQueue.push({
                    participant_id: id,
                    name: getName(tile),
                    speaking: now,
                    t: Date.now(),
                    snapshot: snapshotAllTiles()
                });
            }
        });
        obs.observe(tile, {attributes: true, attributeFilter: ['class'], subtree: true});
        window.__operatorSpeakingObservers[id] = obs;
    });

    return {
        total_observed: Object.keys(window.__operatorSpeakingObservers).length,
        added: added,
        added_names: addedNames,
        local_pid: localPid
    };
}"""


# Drain window.__operatorSpeakingQueue and reset it to []. Returns the
# list of speaking-state events. Safe to call before the observer
# installs — returns [] when the queue doesn't exist yet.
DRAIN_SPEAKING_QUEUE_JS = """() => {
    var q = window.__operatorSpeakingQueue || [];
    window.__operatorSpeakingQueue = [];
    return q;
}"""

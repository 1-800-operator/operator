"""
Shared JS strings for the Google Meet chat-panel DOM scraping.

Both `macos_adapter` (dial/deploy launches a fresh persistent-context
Chrome) and `attach_adapter` (slip CDP-attaches to the user's existing
Chrome) need to inject the same MutationObserver, drain the same JS-side
queue, snapshot the same message IDs for read-back, and walk the same
participant tiles. Keeping the strings here means a fix to Meet's DOM
quirks lands in one place — not two slightly-divergent triple-quoted
literals scattered across adapters.

Mirrors the existing `connectors/captions_js.py` pattern for caption JS.

These are byte-identical extractions from `macos_adapter.py` as of
Phase 14.19.3b.1 — no behavior change. The original adapter imports
these constants and uses them in place of the inline strings.
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
        // text matches "Name + Timestamp". Avoids depending on obfuscated class names.
        const TIME_RE = new RegExp('\\\\d{1,2}:\\\\d{2}\\\\s*(AM|PM)', 'i');
        let sender = '';
        let foundSender = false;
        let node = el;
        for (let d = 0; d < 4 && !foundSender; d++) {
            node = node.parentElement;
            if (!node) break;
            for (const sib of node.children) {
                const t = sib.innerText?.trim();
                if (t && TIME_RE.test(t)) {
                    const lines = t.split('\\n');
                    sender = lines.length >= 2 ? lines[0] : '';
                    foundSender = true;
                    break;
                }
            }
        }
        return {id: msgId, sender: sender, text: text};
    }

    const textarea = document.querySelector('textarea[aria-label="Send a message"]');
    const container = textarea ? textarea.closest('[data-panel-id]') : null;
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


# Drain window.__operatorChatQueue and reset it to an empty array.
# Returns the drained list of {id, sender, text} message dicts. Safe
# to call before the observer attaches — returns [] in that case.
DRAIN_CHAT_QUEUE_JS = """() => {
    const q = window.__operatorChatQueue || [];
    window.__operatorChatQueue = [];
    return q;
}"""


# Best-effort participant name scrape via tile DOM. Meet renders one
# tile per participant with `data-requested-participant-id`; we try a
# few selectors per tile because Meet's class names rotate, plus a
# textContent fallback. Returns a deduped list of plausible names;
# callers must degrade gracefully on [].
GET_PARTICIPANT_NAMES_JS = """() => {
    const tiles = document.querySelectorAll('[data-requested-participant-id]');
    const names = [];
    const seen = new Set();
    tiles.forEach(t => {
        let name = '';
        const labelled = t.querySelector('[data-self-name]');
        if (labelled) name = (labelled.textContent || '').trim();
        if (!name) {
            const aria = t.getAttribute('aria-label') || '';
            if (aria && !aria.includes('More options') && !aria.includes('Menu')) {
                name = aria.trim();
            }
        }
        if (!name) {
            const txt = (t.textContent || '').trim();
            if (txt && txt.length < 60) name = txt;
        }
        if (name && !seen.has(name)) {
            seen.add(name);
            names.push(name);
        }
    });
    return names;
}"""

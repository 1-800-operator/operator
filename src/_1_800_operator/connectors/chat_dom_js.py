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
        return {id: msgId, sender: sender, text: text, t_dom: Date.now()};
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


# Return the operator-runner's Meet display name from the slip Chrome.
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
# aria-labels; non-English locales would silently break. Today the slip
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
# camera-control buttons) and deliberately skipped — slip Chrome's
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

    var tiles = document.querySelectorAll('[data-requested-participant-id]');

    var localPid = '';
    for (var i = 0; i < tiles.length; i++) {
        if (!tiles[i].querySelector('button[aria-label="Reframe"], button[aria-label="Backgrounds and effects"]')) continue;
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
                    t: Date.now()
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

"""
Google Meet captions plumbing — ported verbatim from the voice-preserved branch.

The MutationObserver JS is the battle-tested piece: it scopes to the Captions
region (not document.body), extracts the speaker badge, cleans icon-ligature
noise, dedupes, and batches via setTimeout. Keep the JS unchanged unless Meet's
DOM shifts — it has been tuned against real caption traces.
"""
import logging
import time

log = logging.getLogger(__name__)


# Known junk emitted by Meet UI elements inside the caption region — Material
# icon ligatures and system messages, never speech.
ICON_NAMES = frozenset({
    "mic_off", "mic_none", "keep_outline", "more_vert",
    "aspect_ratio", "visual_effects", "frame_person",
    "arrow_downward", "settings", "close",
})

SYSTEM_PHRASES = (
    "You left the meeting",
    "No one else is in this meeting",
    "Returning to home screen",
)


# MutationObserver scoped to the captions region — not document.body (which
# fires ~130 noise mutations per 3 caption updates). Ported from voice-preserved
# without modification; see captions_adapter.py on that branch for the history.
CAPTION_OBSERVER_JS = """
(() => {
    const BADGE_SEL = ".NWpY1d, .xoMHSc";
    let lastSpeaker = "Unknown";
    let nextNodeId = 1;
    const nodeState = new WeakMap();
    let lastSent = "";
    let lastSentTime = 0;

    const getSpeaker = (node) => {
        const badge = node.querySelector(BADGE_SEL);
        return badge?.textContent?.trim() || lastSpeaker;
    };

    const getText = (node) => {
        const clone = node.cloneNode(true);
        clone.querySelectorAll(BADGE_SEL).forEach(el => el.remove());
        return clone.textContent?.trim() ?? "";
    };

    const send = (node) => {
        const txt = getText(node);
        const spk = getSpeaker(node);
        if (!txt || txt.toLowerCase() === spk.toLowerCase()) return;

        let state = nodeState.get(node);
        if (!state) {
            state = { id: nextNodeId++, lastText: null };
            nodeState.set(node, state);
        }

        if (state.lastText === txt) return;
        state.lastText = txt;
        lastSpeaker = spk;

        const key = spk + "\\0" + txt;
        const now = performance.now();
        if (lastSent === key && now - lastSentTime < 50) return;
        lastSent = key;
        lastSentTime = now;

        window.__onCaption(spk, txt, now);
    };

    // Batch DOM mutations per animation frame to avoid flooding Python.
    let pending = new Set();
    let rafScheduled = false;

    const processPending = () => {
        for (const node of pending) send(node);
        pending.clear();
        rafScheduled = false;
    };

    const REGION_SEL = '[role="region"][aria-label*="Captions"]';

    const attachObserver = (root, label) => {
        new MutationObserver((mutations) => {
            for (const m of mutations) {
                for (const n of m.addedNodes) {
                    if (n instanceof HTMLElement) {
                        pending.add(n);
                    }
                    if (n.nodeType === Node.TEXT_NODE && n.parentElement instanceof HTMLElement) {
                        pending.add(n.parentElement);
                    }
                }
                if (m.type === "characterData" && m.target?.parentElement instanceof HTMLElement) {
                    pending.add(m.target.parentElement);
                }
                if (m.type === "attributes" && m.target instanceof HTMLElement) {
                    pending.add(m.target);
                }
            }
            if (!rafScheduled && pending.size > 0) {
                rafScheduled = true;
                setTimeout(processPending, 0);
            }
        }).observe(root, {
            childList: true,
            characterData: true,
            attributes: true,
            subtree: true,
        });
        window.__onCaption("__operator_diag__", "observer_attached label=" + label, performance.now());
    };

    const region = document.querySelector(REGION_SEL);
    if (region) {
        attachObserver(region, "scoped_region");
    } else {
        let attempts = 0;
        const poll = setInterval(() => {
            const el = document.querySelector(REGION_SEL);
            if (el) {
                clearInterval(poll);
                attachObserver(el, "scoped_region_polled");
            } else if (++attempts > 50) {
                clearInterval(poll);
                attachObserver(document.body, "body_fallback");
            }
        }, 100);
    }
})();
"""


def captions_are_on(page) -> bool:
    """Non-blocking check for captions-on state."""
    try:
        off_btn = page.locator('button[aria-label*="Turn off captions"]')
        if off_btn.is_visible(timeout=300):
            return True
    except Exception:
        pass
    try:
        region = page.locator('[role="region"][aria-label*="Captions"]')
        if region.count() > 0:
            return True
    except Exception:
        pass
    return False


def enable_captions(page) -> bool:
    """Enable captions once. Returns True if captions are confirmed on.

    Shift+C toggles, so only press when captions are confirmed off. Falls back
    to clicking the CC button in the bottom bar if the shortcut fails.
    """
    if captions_are_on(page):
        log.info("captions: already enabled (pre-check)")
        return True

    for i in range(10):
        t_attempt = time.monotonic()
        page.keyboard.down("Shift")
        page.keyboard.press("c")
        page.keyboard.up("Shift")
        page.wait_for_timeout(500)
        if captions_are_on(page):
            log.info(f"captions: enabled via Shift+C (attempt {i+1}, {time.monotonic() - t_attempt:.1f}s)")
            return True
        page.wait_for_timeout(500)

    log.info("captions: Shift+C failed — trying button fallback")
    page.mouse.move(500, 700)
    page.wait_for_timeout(300)
    try:
        cc_btn = page.locator('button[aria-label*="Turn on captions"]')
        cc_btn.wait_for(state="visible", timeout=4000)
        cc_btn.click()
        page.locator('[role="region"][aria-label*="Captions"]').wait_for(timeout=5000)
        log.info("captions: enabled via button fallback")
        return True
    except Exception:
        pass

    log.warning("captions: could not enable (meeting may not support captions)")
    return False


def filter_caption(speaker: str, text: str) -> str | None:
    """Return cleaned text, or None if the caption should be dropped.

    Drops diagnostic sentinels, empty text, icon ligatures, Meet system banners,
    and single-fragment echoes of the speaker name.
    """
    if speaker == "__operator_diag__":
        log.info(f"captions: JS diagnostic — {text}")
        return None
    stripped = (text or "").strip()
    if not stripped:
        return None
    if stripped in ICON_NAMES:
        return None
    if any(stripped.startswith(p) for p in SYSTEM_PHRASES):
        log.info(f"captions: system phrase dropped — {stripped!r}")
        return None
    if stripped.lower() == (speaker or "").lower():
        return None
    return stripped

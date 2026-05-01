# Zoom DOM Inventory

Parity checklist for the Zoom adapter, mirroring `docs/dom-inventory-meet.md`. Selectors verbatim from live DevTools probes; structure walks the lifecycle states a bot traverses from URL → in-meeting → leave.

Probed against `https://us05web.zoom.us/j/<id>?pwd=...` in Chrome (signed-in operator account, Zoom Workplace app installed).

## Status (session 175)

| State | Status | Notes |
|---|---|---|
| 1 — Launcher page | ✅ done | "Join from browser" via text-content match (only stable hook); `zoommtg://` deep-link is browser-chrome modal, ignorable under Playwright. |
| 2a — Device-permission modals | ✅ done | **Two sequential** ReactModal dialogs (camera then mic). "Continue without microphone and camera" is a `<span role="button">`, not a `<button>`. Loop dismiss until detached. |
| 2b — Preview screen + name input | ✅ done | `#input-for-name` is the only stable hook (no placeholder/aria/name attr). Join enable: `button.preview-join-button:not(.zm-btn--disabled)`. |
| 3 — Guest name | ✅ folded into 2b | Bot is never Zoom-authenticated under Playwright, so name input always renders. |
| 4a — In-meeting toolbar | ✅ done | Aria-label hooks throughout (`Leave`, `Share`, `React`, `open the chat panel`, etc.). My initial probe missed Participants because regex was too tight against verbose Zoom labels. |
| 4b — Chat panel | ✅ done | Major divergence: chat input is `div.tiptap.ProseMirror[contenteditable]`, not a textarea. Per-message DOM has `data-userid` + `data-name` on `.chat-item__sender` and a UUID `data-id` on the row for dedup. |
| 4c — More menu | ✅ done | `react-bootstrap` dropdown, `a.dropdown-item[aria-label]`. Build contains: Captions, Whiteboards, Settings, Stop Incoming Video, Reset to default. |
| 4c.1 — Captions submenu | ✅ done | Show Captions + Caption Language nested items. |
| 4c.2 — Caption language modal | ✅ done | First-run only. "Captions will appear in this language for everyone" — meeting-wide side effect. |
| 4c.3 — Caption rendering | ✅ done | `.lt-subtitle-wrap` + `.live-transcription-subtitle__item`. **Native scrape (Option A) viable** for Zoom. Speaker is initials-only; no aria-live; no scrolling history. |
| 4d — Participants count + roster | ✅ done | Count exposed in toolbar button aria-label; full roster in `#participants-ul` with `[role="application"][aria-label="<Name> (Me\|Host),computer audio <state>,video <state>"]`. |

**Architectural findings worth carrying forward (not just selector tables):**
- **TipTap/ProseMirror chat input** (state 4b) breaks Meet-style `page.fill()`. Adapter needs a per-connector text-input strategy: Meet uses `fill()`, Zoom uses `keyboard.type()` against the focused contenteditable (or paste-via-clipboard).
- **Side-panel exclusivity** (state 4d) — chat and participants share the right-side slot. Adapter must restore the chat panel after a roster fetch.
- **Captions are a meeting-wide side effect** on Zoom (per the language-modal copy). Bot enabling captions affects every participant; gate behind explicit per-meeting config, do not enable by default.
- **Captions option-A path** is viable on Zoom for accounts where captions are available; the v1.5 architecture decision can lean Option-A primary, Option-B (tab audio + Whisper) only as fallback for "captions not available" gates.
- **Verbose Zoom aria-labels** ("open the participants list pane,[2] particpants" — sic) — adapter should prefer `aria-label*=` (contains) over exact match. My state-4a regex bug was the canonical example.
- **Build-hashed class names** (`g7nkJFrV` on state 1's "Join from browser") will rotate. Lock selectors to text + tag, not class.
- **Zoom typos** (`particpant`, `particpants`) appear in production aria/class strings — selectors must match verbatim.

---

## State 1 — Launcher page (`https://*.zoom.us/j/<id>?pwd=...`)

URL example: `https://us05web.zoom.us/j/87617459756?pwd=...#success`
Title: `Join from Zoom Workplace app - Zoom`

This is the page Zoom serves when the meeting URL is opened in a browser. The page auto-fires a `zoommtg://` deep-link to launch the desktop app, then surfaces a fallback popover. The bot will never click "Open Zoom Workplace" — it always wants the browser path.

| Action | Selector(s) | Notes |
|---|---|---|
| **Detect launcher popover** | `div[role="dialog"][aria-label="Did not open Zoom Workplace app?"]` (also matches `.zoom-popover`) | Container for the fallback CTAs. Visible after the auto deep-link attempt. |
| **Join from browser** | `button:has-text("Join from browser")` — DOM: `button.zoom-button.zoom-button--lg.zoom-button--secondary` text="Join from browser" | No stable id/aria-label/data-attr. Text match is the only durable hook. The class `g7nkJFrV` is hashed/build-scoped — do not rely on it. |
| **Dismiss launcher popover** | `button[aria-label="Close this popover"]` | Inside the `.zoom-popover` dialog. Use only if we want to keep the page mounted without joining (probably never). |
| **(Avoid) Open Zoom Workplace app** | (auto-fired) `zoommtg:` deep-link | No visible button matched the `open zoom / launch / workplace` regex on probe — the open-app affordance is the auto-fire + browser handler dialog, not a DOM button. We never want to trigger this; if a button surfaces in another build, filter it out. |
| **(Avoid) Download Now** | `a[role="button"]` text="Download Now" | Marketing CTA — never click. |

**Adapter implications:**
- The Chrome-native "Open Zoom Workplace?" external-protocol dialog **is not in the page DOM** (it's a browser-chrome modal), so the probe doesn't see it and Playwright doesn't either. In persistent-context Chromium it auto-dismisses; the `zoommtg://` deep-link silently fails, the page stays put, and Zoom's in-page `.zoom-popover` fallback renders — that's the dialog we drive.
- Wait for `div[role="dialog"][aria-label="Did not open Zoom Workplace app?"]` to be visible, then click `button:has-text("Join from browser")` to enter the in-meeting browser client.
- The `g7nkJFrV` hash class will rotate on Zoom's next deploy. Lock the selector to text + tag, not class.

---

## State 2a — Device-permission dialogs (web client overlay, **two sequential modals**)

URL: `https://app.zoom.us/wc/<id>/join?...&from=pwa&...`
Title: `Zoom meeting on web`

After "Join from browser", the web client (PWA) loads at `app.zoom.us/wc/...` and overlays Zoom-rendered modals asking whether to grant device access. **Zoom shows these as two sequential dialogs**, not one combined dialog:

1. Camera dialog — body: "Do you want people to **see** you in the meeting?"
2. Microphone dialog — body: "Do you want people to **hear** you in the meeting?"

Both use the same `ReactModal__Content` container and the same `pepc-permission-dialog__footer-button` span CTA — only the body text differs. The bot dismisses both via "Continue without microphone and camera" (no audio/video — chat-only).

| Action | Selector(s) | Notes |
|---|---|---|
| **Detect permission modal** | `div.ReactModal__Content[role="dialog"]` | No `aria-label` — match by class. Container also matches `[role="dialog"]`. Two body variants: starts with "Do you want people to see you" (camera) or "Do you want people to hear you" (mic). |
| **Continue without microphone and camera** | `[role="button"].pepc-permission-dialog__footer-button:has-text("Continue without microphone and camera")` | **It's a `<span role="button">`, not a `<button>`** — querying `button` alone misses it. Identical selector for both the camera modal and the mic modal. Class `pepc-permission-dialog__footer-button` is stable Zoom convention. |
| **(Avoid) Use microphone and camera** | _Not surfaced as a separate visible button in this build_ — the modal's primary CTA path likely chains into Chrome's OS-level permission prompt. We bypass it entirely. |

**Adapter pattern:** loop dismissing the permission modal until `div.ReactModal__Content` is detached, with a sane upper bound (e.g. 3 iterations). Don't assume exactly two — Zoom may add a third (screen-share?) on enterprise builds.

## State 2b — Audio/video preview + name input (behind the permission modal)

Once both permission modals are dismissed, the preview screen is the active state. For users not signed in to Zoom (the bot's case — Playwright won't carry a Zoom session), this screen also includes a **name input** that gates the Join button.

| Action | Selector(s) | Notes |
|---|---|---|
| **Name input** | `#input-for-name` | Stable id, `type="text"`. **No `placeholder`, `aria-label`, `name`, or `data-*` attributes** — id is the only stable hook. Typing into it removes the disabled state on the Join button. |
| **Mic toggle (preview)** | `#preview-audio-control-button` (also `button[aria-label="Mute"]`) | Stable id. `aria-label` flips between "Mute" and "Unmute" based on current state. After "Continue without mic/cam" the mic is already off, so the bot probably never needs to touch it here — but keep the selector for robustness. |
| **Cam toggle (preview)** | `#preview-video-control-button` (also `button[aria-label="Stop Video"]`) | Stable id. `aria-label` flips between "Stop Video" and "Start Video". |
| **More video controls** | `button[aria-label="More video controls"].preview__toggle` | Disclosure for additional video settings. Don't click. |
| **Backgrounds (preview)** | `#preview-bg-control-button` | Don't click. |
| **Back** | `button.btn-back-pwa` text="Back" | Returns to launcher. Don't click. |
| **Join (disabled)** | `button.zm-btn.preview-join-button.zm-btn--disabled` text="Join" | Initial state when permission modals were just dismissed and/or name field is empty. The `disabled` and `zm-btn--disabled` classes both apply. |
| **Join (enabled)** | `button.preview-join-button:not(.zm-btn--disabled)` text="Join" | Both `disabled` / `zm-btn--disabled` modifier classes drop off once a non-empty name has been entered. This class delta is the ready-signal — equivalent to Meet's "Ask to join" button enable. |
| **(Defer) Send Report** | Untyped `<button>` text="Send Report" | Footer item, irrelevant. |

**Adapter implications:**
- The bot will not be signed into Zoom under Playwright, so the name input **will** be present. Type the agent's display name (e.g. `agents/<bot>/config.yaml::agent.name`) into `#input-for-name` before attempting to click Join.
- Wait for `button.preview-join-button:not(.zm-btn--disabled)` to be enabled, then click.
- The Chrome-profile login state is irrelevant here — Zoom's web client tracks its own auth, independent of the Google account in Chrome.

## State 4a — In-meeting toolbar (baseline, panels closed)

URL: `https://app.zoom.us/wc/<id>/join?...&from=pwa&...` (URL stays the same after Join — no nav)
Title: `Zoom meeting on web`

The bottom toolbar after entering the meeting. The bot joined **without audio or video**, so the mic/cam buttons are in their "join audio" / "start video" affordance state, not the in-call mute toggle state.

| Action | Selector(s) | Notes |
|---|---|---|
| **Audio (join audio)** | `button.join-audio-container__btn[aria-label="audio"]` text="Audio" | Pre-audio-join state. Lowercase `aria-label="audio"` (Zoom's quirk). Bot never clicks — chat-only. |
| **Video (start video)** | `button.send-video-container__btn[aria-label="Video"]` text="Video" | Pre-video-start state. Bot never clicks. |
| **Leave** | `button[aria-label="Leave"].footer-button__button` text="Leave" | Stable. Equivalent to Meet's leave call action. |
| **Open chat panel** | `button[aria-label="open the chat panel"]` text="Chat" | `aria-label` is lowercase. Likely flips to `aria-label="close the chat panel"` when open — confirm in next probe. |
| **Share screen** | `button.sharing-entry-button-container[aria-label="Share"]` text="Share" | Stable. |
| **React** | `button[aria-label="React"]` text="React" | Reactions menu (emoji palette). Bot doesn't use. |
| **More** | `button.footer-button-base__button` text="More" — **no aria-label** | Use text-content match. Likely contains: Participants, Captions, Recording, Apps, Whiteboard. Need a follow-up probe with this menu open. |
| **Participants (open panel + count)** | `button[aria-label^="open the participants list pane"].footer-button__button` text="`<N>`Participants" (e.g. `"2Participants"`) | **Surfaces only when count ≥ 2** (when the bot is alone, this button is hidden). The aria-label embeds the count: `"open the participants list pane,[2] particpants"` (sic — Zoom typo `particpants`). Two ways to read count: `aria-label` regex `\[(\d+)\]` or text-content prefix-digits. **For `get_participant_count()` the bot doesn't need to open the panel** — count is in the button itself. If the button is absent, count == 1 (bot only). |
| **Participants Settings (sub-toggle)** | `button[aria-label="Participants Settings"].footer-participants-button__toggle-button` | Sibling sub-toggle next to the Participants button. Probably opens privacy / lock-meeting actions. Bot doesn't use. |
| **AI Companion** | `button[aria-label^="Open AI Companion panel"]` text="AI Companion" | Zoom's first-party AI assistant panel. On by default in this build. Bot doesn't use; ignore. |
| **Meeting information (top-left)** | `button#meeting-info-indication[aria-label="Meeting information"]` | Top-left info icon (host name, meeting id). Bot doesn't use. |
| **Picture-in-picture** | `button#fullscreen-pip-btn[aria-label="Enter Pip"]` text="Pop Out" | Pop video into floating window. Bot doesn't use. |
| **More menu host (state probe)** | `div[role="group"][aria-label="More meeting control "].dropdown-toggle.btn-group` | Parent of the More button. Adds class `.show` when the dropdown is open — use this as the open/closed state signal (`.dropdown-toggle.show`). |
| **Captions** | (under More menu — see state 4c) | |

**Common toolbar selector pattern:** all bottom-toolbar buttons share the class prefix `footer-button-base__button` — useful for sanity-checking that an element is a toolbar item. The class `ax-outline` indicates accessibility focus styling.

**Sub-state probes captured:**
- **4b — Chat panel open:** ✅ input + send + container + history row selectors captured.
- **4c — More menu open:** ✅ captured (see below).
- **4d — Participants surface:** ✅ both count and roster captured (see below).

## State 4c — More menu open

Triggered by clicking the toolbar **More** button. Renders as a Bootstrap-style dropdown using `react-bootstrap` (`data-rr-ui-dropdown-item` is the giveaway).

| Action | Selector(s) | Notes |
|---|---|---|
| **Menu item — canonical pattern** | `a.dropdown-item[role="button"][aria-label="<label>"]` | Each item is an `<a>` with `role="button"` and a stable `aria-label`. Also has empty `data-rr-ui-dropdown-item` attribute. Wrapped in `div.more-button__item-box[role="button"]` with the same text content. Click either; the `<a>` is the canonical handler. |
| **Captions (opens submenu)** | `a.dropdown-item[aria-label="Captions"]` | **Does not toggle captions on directly.** Opens a sub-popover with two child items: Show Captions, Caption Language. See state 4c.1 below. |
| **Whiteboards** | `a.dropdown-item.more-btns__dropdown[aria-label="Whiteboards submenu "]` | Has trailing space in aria-label. Submenu — has `id="dropdown"` toggle button child. Bot doesn't use. |
| **Settings** | `a.dropdown-item[aria-label="Settings"]` | Opens Zoom in-meeting settings (audio device picker, etc.). Bot doesn't use. |
| **Stop Incoming Video** | `a.dropdown-item[aria-label="Stop Incoming Video"]` | Disables incoming video rendering — performance setting. The bot can opt into this on join to save CPU on participant grids it never renders. |
| **Reset to default** | `a.dropdown-item[aria-label="Reset to default"]` | Restores Zoom UI defaults. Bot doesn't use. |

(In this build the menu contains: Captions, Whiteboards (submenu), Settings, Stop Incoming Video, Reset to default. No Participants/Recording/Apps/Notes/Raise Hand.)

### State 4c.1 — Captions submenu (More → Captions hover/click)

Clicking the Captions item in More opens a secondary dropdown attached to the same menu surface (no popper root, just nested dropdown-items).

| Action | Selector(s) | Notes |
|---|---|---|
| **Show Captions** | `a.dropdown-item[aria-label="Your caption settings grouping Show Captions"]` text="Show Captions" | The actual toggle. Click this to enable captions. The aria-label phrasing ("Your caption settings grouping...") is Zoom's accessibility convention for nested-menu announcements. |
| **Caption Language** | `a.dropdown-item.new-LT__between[aria-label="Host controls grouping My Caption Language"]` text=`"Caption Language:English"` | Opens a deeper language picker. The aria-label says "Host controls grouping" but the item is visible to non-hosts — probably means hosts have stronger language-changing privileges, but guests can pick their own caption rendering language. Bot's default English is fine; ignore unless multi-lingual support is in scope. |

(No third item like "captions not available" or "ask host to enable" — confirms **captions are available client-side in this free guest meeting**, which is meaningful for the v1.5 architecture decision: Option A — native scrape — is viable on Zoom for this meeting type at minimum.)

### State 4c.2 — Caption-language first-run modal

Triggered the first time **Show Captions** is clicked in this meeting (subsequent clicks toggle without the prompt). Modal text reads "Set the caption language for this meeting" / "Captions will appear in this language for everyone." Default selection: English. **Note the "for everyone" wording — this is a meeting-wide setting, not per-user.** As a guest in a meeting with no other host-active captions config, the dialog still fires; possibly host-level on multi-host calls.

| Action | Selector(s) | Notes |
|---|---|---|
| **Modal** | `div[role="dialog"].ReactModal__Content` containing text starting with "Set the caption language for this meeting" | Same `ReactModal__Content` shell Zoom uses everywhere — disambiguate by text. |
| **Language combobox** | `input#react-select-2-input[role="combobox"][aria-label="Caption Language"].transcription-language__input` | React-select widget. **The `react-select-2` numeric suffix in the id is auto-generated** — class `transcription-language__input` is the stable hook. Default value pre-selected = English. |
| **Cancel** | `button.zm-btn-legacy.zm-btn--default[text="Cancel"]` | Closes modal without enabling captions. |
| **Save** | `button.zm-btn-legacy.zm-btn--primary.zm-btn__outline--blue[text="Save"]` | Confirms language and enables captions. **Adapter path:** accept default English by clicking Save without touching the combobox. |

**Adapter implications:**
- On the first Show Captions click in any meeting, expect this modal. Auto-click Save (English default) unless config specifies otherwise.
- The modal's "for everyone" copy is a behavioral detail worth surfacing in user docs — guests enabling captions affect the whole room. The bot enabling captions is itself a meeting-side-effect, which we should be intentional about.
- A "Captions enabled" toast notification fires after Save but **auto-dismisses too fast to reliably probe** (~1–2s). Don't use it as a verification signal. To verify captions are running, either: (a) re-open the More → Captions submenu and check whether "Show Captions" has flipped to "Hide Captions" / has an aria-pressed state, or (b) poll directly for caption-text DOM rendering after speech.

### State 4c.3 — Caption rendering (captions enabled + speech occurring)

Once Save dismisses the language modal and someone speaks, captions render in a draggable subtitle box overlaid on the meeting view.

| Action | Selector(s) | Notes |
|---|---|---|
| **Caption wrap (stable parent)** | `div.lt-subtitle-wrap` | Outer container; always present once captions are enabled, even between utterances. Best target for a `MutationObserver` to listen for caption changes. |
| **Caption box (draggable, ephemeral per utterance)** | `div.live-transcription-subtitle__box.ax-outline-blue.react-draggable` | Visible subtitle bubble. Text content is `"<initials> <speech>"` — concatenates the speaker's avatar initials with their words. Class `react-draggable` confirms the box can be repositioned by the user. |
| **Caption text (speech only)** | `span.live-transcription-subtitle__item` | The clean speech text without the avatar prefix. Use this for `read_captions()` body extraction. |
| **Speaker attribution** | Only the avatar initials are exposed in the box's text (e.g. `"JS Hey, this is a test"`). **No `data-userid` or full-name attribute on the box itself.** | Significant limitation for "who said what" attribution. Resolution path: (a) cross-reference initials against the participants roster (`get_participant_names()` returns `<Name>` and `<Initials>` is `<Name>`-derived); (b) attempt to capture an aria-label or data attribute on the avatar element by descending one more level (pending probe — only the `__box` and `__item` were caught here, the avatar likely sits as a sibling div but wasn't visible to the matchers in this run). |
| **Live-region status** | **None** — neither the box nor the wrap has `aria-live`. | Zoom accessibility shortfall. Means we can't rely on screen-reader-style live updates; **must use a `MutationObserver` on `.lt-subtitle-wrap` to detect new captions.** |
| **History** | The caption box shows only the **latest utterance**; no scrolling history is rendered in the DOM. | For full transcript, the bot must accumulate captions client-side as the box mutates. Each utterance is a discrete render — adapter buffers. |

**Architecture-decision implication:** Native scrape (Option A from the v1.5 captions decision) is **viable** for Zoom for accounts/meetings where captions are available. Caveats: speaker attribution is initials-only without an extra mapping step; transcript history must be accumulated by us, not pulled from a backlog.

## State 4d — Participants panel open

Triggered by clicking the toolbar Participants button. Opens a side panel with the roster.

| Action | Selector(s) | Notes |
|---|---|---|
| **Panel header (count, alt source)** | `span[aria-label="Participants (2)"]` text="Participants (2)" | Third place the count surfaces (toolbar button text, toolbar aria-label, panel header). All three update live. |
| **Close panel** | `button[aria-label="Close"].particpant-header__close-right` | Note Zoom typo: `particpant` (single 'i') — verbatim selector. |
| **Pop out panel** | `button[aria-label="Pop Out"]` | Detaches panel into floating window. Bot doesn't use. |
| **Roster (scrollable list)** | `div#participants-ul[role="list"][aria-label="Participants list"].participants-list-container` | Stable id `#participants-ul`. Class includes `ReactVirtualized__Grid ReactVirtualized__List` — rows are virtualized and only render when in viewport. **For rosters > visible window, scroll the container to enumerate everyone.** For 1-on-1 / small meetings this isn't a concern. |
| **Roster row** | `div.item-pos.participants-li[id^="participants-list-"][role="application"]` | `id` is sequential (`participants-list-0`, `participants-list-1`, …). The aria-label is the data-rich source. |
| **Roster row aria-label format** | `"<Name> [(Me)\|(Host)\|(Co-host)?],computer audio <muted\|unmuted>,video <on\|off>"` | Examples: `"claude (Me),computer audio unmuted,video off"`; `"Jojo Shapiro (Host),computer audio unmuted,video on"`. **Single comma-separated string carrying name, self-flag, host-flag, audio state, video state.** Parse by splitting on `,`, trimming. The leading segment (before first comma) holds `<Name> (<role>)` where role is one of `(Me)`, `(Host)`, `(Co-host)`, or absent for a regular guest. |
| **Roster row text content** | e.g. `"C claude (Me)"`, `"JS Jojo Shapiro (Host)"` | Format: `"<Initials> <Name> (<role>?)"` where initials are 1–2 letters from the avatar. Lower-fidelity than aria-label — prefer aria-label for parsing. |
| **Self detection** | aria-label segment matches `\s*\(Me\)\b` | The bot identifies its own row this way. Useful to filter self out of `get_participant_names()`. |
| **Host detection** | aria-label segment matches `\s*\(Host\)\b` | Useful for any host-aware logic (e.g. only respond to host commands in some workflows). |

**Adapter implications:**
- `get_participant_count()`: prefer the toolbar button (zero-cost — no panel toggle). Read from `aria-label` regex `\[(\d+)\]` or text-content prefix-digits. **If button is absent, count = 1.**
- `get_participant_names()`: must open the panel (toolbar button), iterate `div[id^="participants-list-"]`, parse the aria-label for `<Name>` and `(Me)/(Host)` flags. Filter out the `(Me)` self-row. **Close the panel after** to keep the chat panel re-takeable as the sidebar — Zoom's web client only allows one side-panel at a time.
- ReactVirtualized: for rosters > visible window, scroll `#participants-ul` and re-query. Out of scope for v1 (chat-bot use case is 1-on-1 / small meetings).

**Conspicuous absences in this build:** no `Participants`, `Recording`, `Apps`, `Notes`, or `Raise Hand` menu items. Likely host-only or paid-tier features. For the bot:
- **Participants list** must be sourced from somewhere else — possibly the always-visible gallery (mid-screen video tile names) or a separate panel toggle not yet probed. Add to pending probes (4d).
- **Recording** absent means Zoom's free guest path forbids client-side recording — fine, the bot doesn't record.

**Adapter implications:**
- Click pattern: `button:has-text("More")` to open, then `a.dropdown-item[aria-label="Captions"]` (etc.) for the target item. The dropdown closes automatically on item click.
- "Stop Incoming Video" is a useful side-effect tweak — bot doesn't watch video streams, so disabling renders saves CPU and reduces page churn during long meetings.

## State 4b — Chat panel open

Triggered by clicking the toolbar **Chat** button. Panel opens as a docked side container.

| Action | Selector(s) | Notes |
|---|---|---|
| **Chat panel outer container** | `div#chatContainer.chat-container__chat-control` | Stable id (`#chatContainer`). Mounts the whole chat sidebar. |
| **Chat panel container** | `div.chat-container.window-content-bottom.chat-container--normal` | Inner wrapper. The `--normal` modifier may flip if the panel is popped out / detached — TBD. |
| **Chat list (scrollable)** | `div.chat-container__chat-list` | The scrollable region holding the message stream. Equivalent to Meet's `[jsname="xySENc"]` chat container. |
| **Chat message list (inner)** | `div.chat-item__chat-info[role="application"][aria-label="Chat Message List"]` | The actual message stream. `role="application"` is unusual — Zoom uses it to take over keyboard handling inside the list, which means Playwright keyboard events fired against ancestors may be swallowed. Query messages by descending into this container. |
| **System placeholder row (filter out)** | `div.chat-item__pmc[data-id="msgId-placeholder"]` | The "Messages addressed to 'Meeting Group Chat' will also appear..." informational row. **Filter by `data-id="msgId-placeholder"`** when iterating the chat list — it's not a real message. |
| **Chat header (meeting topic)** | `span.chat-header__meeting-topic` (e.g. text="Jojo Shapiro's Zoom Meeting") | Useful for sanity-checking we're in the right meeting. |
| **Recipient label / scope** | `div.chat-rtf-box__chat-textarea-wrapper` text="to: Meeting Group Chat" | For guests, the only chat scope is the persistent "Meeting Group Chat" — no "Everyone" / DM picker surfaced. Hosts probably get a recipient dropdown — not captured here. |
| **Guest banner** | `div.chat-header__title-wrap--guest` text starts with "You're chatting as a guest" | Surfaces only for guests. Confirms guest scope: messages are echoed to a persistent Zoom Team Chat thread for that meeting. **Open question:** does this echo affect what we `read_chat()`? The in-panel history we polled showed only the placeholder row, so apparently the echo is one-way (out, not in). Re-verify after sending a message. |
| **Distraction: device-permission notification** | `div.notification-message-wrap` text starts with "Please enable access to your microphone and camera" | A non-modal toast inside the chat panel area asking the user to grant device permissions (because we picked "Continue without"). Close button: `i.notification-message-wrap__close[role="button"][aria-label="close"]`. Bot can ignore — doesn't block chat — but worth dismissing on join to keep the chat list clean. |
| **Chat input** | `div.tiptap.ProseMirror[contenteditable="true"]` | **Major parity divergence from Meet** — Meet's chat is a plain `<textarea>`, Zoom's is a TipTap/ProseMirror rich-text editor. Playwright's `locator.fill()` will not work — must use `locator.pressSequentially(text)` (slower but real key events) or `locator.evaluate(el => el.textContent = '...')` + dispatch `input` event. Cleanest approach: `locator.click()` to focus, then `page.keyboard.type(text)`. |
| **Send button (disabled, empty input)** | `button.chat-rtf-box__send.chat-rtf-box__send--disabled[aria-label="send"]` | |
| **Send button (enabled, after typing)** | `button.chat-rtf-box__send:not(.chat-rtf-box__send--disabled)[aria-label="send"]` | Same `--disabled` modifier-class pattern as Join. Class delta is the ready-signal. |
| **Close chat panel** | (tentatively the toolbar Chat button flips to `aria-label="close the chat panel"` — confirm in next probe) | Did not surface a dedicated close button in the chat header within the captured slice. May exist further down — re-probe after sending a message to see the full panel. |
| **Chat history row** | `div.chat-item-container[id^="chat-item-container-"][data-id]` (e.g. `#chat-item-container-1`, `data-id="1-D6D48962-895C-427C-A367-36B5A1AED608"`) | Each message is a `chat-item-container` wrapper. `id` is sequential per-session (`chat-item-container-N`); `data-id` is a durable UUID — **use `data-id` as the dedup key for `read_chat()` polling**. Inner wrapper: `div.chat-item-position`. Filter out `[data-id="msgId-placeholder"]` (informational row). |
| **Sender name** | `span.chat-item__sender[data-userid][data-name]` (descendant of the row) | Use `data-name` attr (`"Jojo Shapiro"`) for the display name; use `data-userid` (`"16778240"`) for stable identity. **`data-userid` is the bot's own-message filter** — store the bot's own userid after join and dedupe outbound messages. |
| **Timestamp** | `span.new-chat-item__chat-info-time-stamp` text="03:54 PM" | Wall-clock time only (no date). For Operator's append-only `MeetingRecord`, capture the receive timestamp from the polling loop instead — Zoom's row time is for display, not ordering. |
| **Body (preferred)** | `div.chat-rtf-box__display` text="hi" | Content-only. Cleanest source for `read_chat()` body extraction. |
| **Body (alternate / dedup-keyed)** | `div.new-chat-message__text-box[id="<row-data-id>"]` (e.g. `id="1-D6D48962-895C-427C-A367-36B5A1AED608"`) | Inner div carries the **same UUID** as the row's `data-id`. Useful as a sanity check that we picked the right row. |
| **Body (fully-formatted aria fallback)** | `div.new-chat-message__container[role="row"][aria-label]` — example aria-label: `"Jojo Shapiro to Everyone, 03:54 PM, hi"` | The aria-label is a pre-formatted `"<sender> to <recipient>, <time>, <body>"` string. Confirms the in-meeting **recipient defaults to "Everyone"** (despite the input wrapper text saying "to: Meeting Group Chat" — that's the persistent-thread mirror, not the in-meeting scope). Bot doesn't need to change recipient. |
| **Avatar (ignore)** | `div.chat-item__user-avatar` text="JS" (initials) | Decorative. |
| **Reply / React / Vote buttons (ignore)** | `button[aria-label="Reply"]`, `button[aria-label="Add reaction"]`, `.chat-vote-row__button` | Hover affordances on each row. Bot doesn't use. |
| **Note: send-button class flips back** | After sending, `chat-rtf-box__send` returns to `--disabled` once the input is cleared. So `--disabled` ↔ "input empty"; absence of `--disabled` ↔ "input has content". |

**Adapter implications:**
- The TipTap contenteditable input means we need a Zoom-specific `send_chat()` path; Meet's textarea-based path won't work as-is. Plan: thin per-connector text-input strategy in the adapter (Meet uses `fill()`, Zoom uses `keyboard.type()` or paste).
- Persistent-chat echo to Zoom Team Chat: pre-prod, document and observe; may need a guardrail later if duplicates appear in history.

## State 5 — Leave / end-of-meeting

The toolbar **Leave** button (`button[aria-label="Leave"].footer-button__button`) is the only entry point. The confirmation modal that follows was not probed — adapter implementer should capture it inline when wiring `leave()` (one click + `document.querySelector('div[role="dialog"].ReactModal__Content')`). Expected shape: ReactModal with "Leave Meeting" / "Cancel" buttons in Zoom's `zm-btn-legacy` family.

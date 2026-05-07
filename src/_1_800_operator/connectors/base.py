class MeetingConnector:
    """Abstract meeting-platform interface — what ChatRunner needs from a connector.

    Implemented by MacOSAdapter (dial mode — fresh persistent-context Chrome),
    AttachAdapter (slip mode — CDP-attach to dedicated slip Chrome), and
    LinuxAdapter (headless Chromium). ChatRunner is platform-agnostic and
    consumes only this surface; everything Meet-specific lives in the
    adapters and `connectors/{captions,chat_dom}_js.py`.

    Two tiers of methods: lifecycle + chat (`join`, `send_chat`, `read_chat`,
    `leave`) raise NotImplementedError on the base — adapters MUST
    implement; participant + caption + connectivity getters return safe
    defaults so adapters can no-op when the platform doesn't expose them.
    """

    def __init__(self):
        self.join_status = None  # Set by join(); see session.JoinStatus

    def join(self, meeting_url):
        raise NotImplementedError

    def send_chat(self, message):
        """Post a message to chat. Returns the new message's stable ID
        (e.g. data-message-id, channel-message-tuple) or None if the
        adapter couldn't capture it. ChatRunner uses the ID to add the
        sent message to its seen set so the same DOM event picked up by
        the read path is not reprocessed as a new user message.
        """
        raise NotImplementedError

    def read_chat(self):
        """Return a list of new chat messages since last call.

        Each message is a dict: {"id": str, "sender": str, "text": str}.
        Returns an empty list if no new messages.
        """
        raise NotImplementedError

    def get_participant_count(self):
        """Return the number of participants currently in the meeting.

        Returns 0 if the count cannot be determined.
        """
        return 0

    def get_participant_names(self):
        """Return display names of participants currently in the meeting.

        Best-effort — connectors that can't scrape names return []. Includes
        the bot's own name; callers that want only "others" should filter.
        """
        return []

    def is_connected(self):
        """Return True if the browser session is still alive.

        Returns False when the browser has exited (crash, page loss, or
        after leave() has completed). ChatRunner polls this to detect
        unexpected disconnects and exit the loop cleanly.
        """
        return True

    def set_caption_callback(self, fn):
        """Register fn(speaker, text, timestamp) for caption updates.

        Optional — connectors that don't support captions may no-op. Must be
        called before join() when supported.
        """
        pass

    def leave(self):
        raise NotImplementedError

from ... import DOWNLOAD_DIR, user_data
from . import (
    BatchTrackerMixin,
    MediaPipelineMixin,
    MultiLinkMixin,
    SettingsResolverMixin,
)
from ..telegram.message_utils import chat_of


class TaskConfig(
    SettingsResolverMixin,
    BatchTrackerMixin,
    MediaPipelineMixin,
    MultiLinkMixin,
):
    """Per-task state + behavior, composed from four responsibility mixins.

    Subclasses (via ``TaskListener``) must set ``self.message`` and
    ``self.client`` *before* calling ``super().__init__()``.
    """

    def __init__(self):
        # A bulk child shares one telegram message with all its siblings, so it
        # cannot take its identity from ``message.id``; the dispatcher hands it a
        # synthetic mid instead. ``cmd_msg_id`` keeps the *real* message id for
        # the places that need to reply to / fetch the command message.
        self.mid = getattr(self, "_forced_mid", 0) or self.message.id
        self.bulk_child = bool(getattr(self, "_forced_mid", 0))
        self.cmd_msg_id = self.message.id
        self.cmd_text = getattr(self, "_cmd_text", "") or (
            self.message.text or self.message.caption or ""
        )
        # A command reaching a handler was sent by someone: a user, or the chat
        # itself for a channel post or an anonymous admin. ``user_id`` on the
        # next line has always assumed that, so it is stated here instead.
        #
        # The second suppression is pyrogram's ``Chat.id``, which it declares
        # optional for the chats it builds from a partial update. A chat that
        # posted a message is not one of those.
        self.user = self.message.from_user or self.message.sender_chat  # pyrefly: ignore[bad-assignment]
        self.user_id = self.user.id  # pyrefly: ignore[bad-assignment]
        self.user_dict = user_data.get(self.user_id, {})
        self.clone_dump_chats = {}
        self.copy_preset = ""
        # What this task uploaded, one entry per copy command, filled by the
        # uploader as messages go out and written to the database when the
        # task completes. Empty for a task with no database configured.
        self.copy_units = []
        self.dir = f"{DOWNLOAD_DIR}{self.mid}"
        self.up_dir = ""
        self.link = ""
        self.up_dest = ""
        self.tag = ""
        self.name = ""
        self.subname = ""
        self.name_sub = ""
        self.thumbnail_layout = ""
        self.folder_name = ""
        self.split_size = 0
        self.max_split_size = 0
        self.multi = 0
        self.size = 0
        self.subsize = 0
        self.proceed_count = 0
        self._alldebrid_magnet_id = 0
        self._torbox_torrent_id = 0
        self._torbox_web_id = 0
        # ponytail: is_leech is always True (leech-only branch). Kept as
        # instance attr for uploader/status code; remove if bot gains other modes.
        self.is_leech = True
        self.is_qbit = False
        self.is_ytdlp = False
        self.is_alldebrid = False
        self.is_torbox = False
        self.stream_upload = False
        self.equal_splits = False
        self.user_transmission = False
        self.hybrid_leech = False
        self.extract = False
        self.compress = False
        self.select = False
        self.seed = False
        self.join = False
        self.sample_video = False
        self.convert_audio = False
        self.convert_video = False
        self.screen_shots = False
        self.is_cancelled = False
        self.force_run = False
        self.force_download = False
        self.force_upload = False
        self.is_torrent = False
        self.as_med = False
        self.as_doc = False
        self.is_file = False
        self.bot_trans = False
        self.user_trans = False
        self.is_rss = getattr(self.message, "_rss_trigger", False)
        self.progress = True
        self.ffmpeg_cmds = None
        self.chat_thread_id = None
        self.subproc = None
        # "" and not None: ``_apply_args`` overwrites this with the ``-t``
        # argument (also "" when absent) before anything reads it, and the one
        # reader that does not go through a truth test hands it to
        # ``is_telegram_link``, which calls ``startswith`` on it.
        self.thumb = ""
        self.excluded_extensions = []
        self.included_extensions = []
        self.files_to_proceed = []
        # A chat without a type is no evidence of a super chat, so it is treated
        # as the plain chat it looks like rather than crashing the task here.
        chat_type = chat_of(self.message).type
        self.is_super_chat = chat_type is not None and chat_type.name in [
            "SUPERGROUP",
            "CHANNEL",
            "FORUM",
        ]

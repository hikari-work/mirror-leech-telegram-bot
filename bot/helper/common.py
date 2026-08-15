from .. import DOWNLOAD_DIR, user_data
from .task_config import (
    BatchTrackerMixin,
    MediaPipelineMixin,
    MultiLinkMixin,
    SettingsResolverMixin,
)


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
        self.mid = self.message.id
        self.user = self.message.from_user or self.message.sender_chat
        self.user_id = self.user.id
        self.user_dict = user_data.get(self.user_id, {})
        self.clone_dump_chats = {}
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
        self.thumb = None
        self.excluded_extensions = []
        self.included_extensions = []
        self.files_to_proceed = []
        self.is_super_chat = self.message.chat.type.name in [
            "SUPERGROUP",
            "CHANNEL",
            "FORUM",
        ]

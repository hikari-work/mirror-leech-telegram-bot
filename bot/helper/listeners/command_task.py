"""The part of a command-started task that ``/leech`` and ``/ytdl`` share.

Both build their listener the same way -- same parameters, same assignments, the
same two forced-identity fields ``TaskConfig.__init__`` reads -- and both then
copy the parsed arguments onto themselves field by field. Two copies of that is
two places to edit whenever an option is added, which is how the lists came to
disagree about which options each command honours.
"""

from ... import LOGGER
from ..telegram.message_utils import chat_of
from ..util.task_args import COMMON_ARG_FIELDS, dump_args
from .task_listener import TaskListener

ACTIVE_TASK_SCHEMA = 1
"""Version of an ``active_tasks.data`` document.

A restart runs ``update.py`` before the bot comes back, so the code that reads a
row may not be the code that wrote it. A reader that does not recognise this
number refuses the row and leaves the task to the incomplete-task notifier
rather than rebuilding a listener out of fields it only half understands.
"""


class CommandTask(TaskListener):
    """A :class:`TaskListener` built from a chat command and its parsed args.

    The bulk dispatcher constructs children positionally, so the parameter order
    here is what ``dispatch_bulk`` and ``run_multi`` pass.
    """

    def __init__(
        self,
        client,
        message,
        is_qbit=False,
        same_dir=None,
        bulk=None,
        multi_tag=None,
        options="",
        mid=0,
        cmd_text="",
    ):
        if same_dir is None:
            same_dir = {}
        if bulk is None:
            bulk = []
        self.message = message
        self.client = client
        self.multi_tag = multi_tag
        self.options = options
        self.same_dir = same_dir
        self.bulk = bulk
        # read by TaskConfig.__init__ to override the message-derived identity
        self._forced_mid = mid
        self._cmd_text = cmd_text
        super().__init__()
        # after super(), which defaults it to False
        self.is_qbit = is_qbit

    def apply_options(self, args):
        """Re-settle this task from *args* after something changed them.

        The option keyboard mutates the parsed arguments and calls this, so the
        flags it toggles travel the same one mapping everything else does. The
        contract is the one ``_apply_args`` already has: the args are the whole
        answer, and applying them twice has to leave the task where applying
        them once did.
        """
        self._apply_args(args)

    def _apply_args(self, args):
        """Transfer the arguments both commands accept onto *self*.

        The mapping is the one the parser filled them from, so an option only has
        to be named once to travel all the way from the command to the task.
        Subclasses assign their own flags first and hand back to this one last,
        so the destination settled at the bottom is the final word.
        """
        for attr in COMMON_ARG_FIELDS.values():
            setattr(self, attr, getattr(args, attr))
        self.folder_name = args.folder_name
        self.multi = args.multi
        # The flags override the destination the config chose, and -s3 wins if
        # both are given. Which of the two the user typed last is not something
        # the parser records, so a rule that can be stated beats one that
        # depends on the order of tokens. Writing all three cases -- rather than
        # only the flagged ones -- is what lets this run more than once: the
        # option keyboard re-applies the args after every toggle, and a task
        # that was put on s3 by hand has to be able to come back off it.
        if args.is_s3:
            self.destination = "s3"
        elif args.is_tg:
            self.destination = "tg"
        else:
            self.destination = self.configured_destination
        if self.destination == "s3":
            # Both of these exist to feed the telegram uploader, and both do
            # their work *during* the download: -su picks TelegramUploader
            # inside DirectListener's streaming path, before any destination is
            # chosen, and -ss moves a single-file download into a screenshot
            # folder of its own, which is then no longer the folder the
            # completion message links to. Turning them off is not politeness
            # to a flag that would be ignored anyway -- leaving either on breaks
            # the upload rather than the option.
            self.stream_upload = False
            self.screen_shots = False

    # ── restart recovery ────────────────────────────────────────────

    async def record_active_task(self, args, handler, engine, engine_tag="", engine_dir=""):
        """Leave behind what a restart would need to pick this task back up.

        Written where the download is handed to its engine and nowhere else. A
        task still parked on the option keyboard holds no engine job and
        nothing on disk, so there is nothing to come back to; a task that has
        reached this point has already been through ``before_start`` and had
        its options settled.

        The arguments are the ones the keyboard left behind rather than the
        command text: a user who toggled ``-s3`` or picked a copy preset changed
        the arguments without changing the message they sent, and re-parsing the
        text on the way back would quietly undo those choices.

        Never raises. A database that is down costs the task its resumability,
        which is worth far less than the task itself -- and this runs on the
        path that starts the download.
        """
        try:
            from ..storage.db_handler import database

            await database.add_active_task(
                self.mid,
                chat_of(self.message).id,
                self.cmd_msg_id,
                self.user_id,
                self.tag,
                {
                    "schema": ACTIVE_TASK_SCHEMA,
                    "handler": handler,
                    "engine": engine,
                    # What the engine files the job under, for the engines that
                    # have such a thing: qBittorrent keeps the task id as its
                    # tag. aria2 is matched on its ``dir`` instead, because a
                    # magnet's gid changes the moment its metadata arrives.
                    "engine_tag": engine_tag,
                    # The path as it was handed to the engine, not one computed
                    # again on the way back: aria2 is re-attached by matching
                    # this against the ``dir`` its downloads were added with.
                    "engine_dir": engine_dir,
                    "args": dump_args(args),
                    "cmd_text": self.cmd_text,
                    "folder_name": self.folder_name,
                    "multi": self.multi,
                    "multi_tag": self.multi_tag,
                    # A resolved direct link is a dict carrying the file list,
                    # the headers and the size, and rebuilding it means scraping
                    # the page again -- possibly after the session that made it
                    # work has expired.
                    "link": self.link,
                },
            )
        except Exception as e:
            LOGGER.error(f"Unable to record active task {self.mid}: {e}")

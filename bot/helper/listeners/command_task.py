"""The part of a command-started task that ``/leech`` and ``/ytdl`` share.

Both build their listener the same way -- same parameters, same assignments, the
same two forced-identity fields ``TaskConfig.__init__`` reads -- and both then
copy the parsed arguments onto themselves field by field. Two copies of that is
two places to edit whenever an option is added, which is how the lists came to
disagree about which options each command honours.
"""

from ..ext_utils.task_args import COMMON_ARG_FIELDS
from .task_listener import TaskListener


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

    def _apply_args(self, args):
        """Transfer the arguments both commands accept onto *self*.

        The mapping is the one the parser filled them from, so an option only has
        to be named once to travel all the way from the command to the task.
        """
        for attr in COMMON_ARG_FIELDS.values():
            setattr(self, attr, getattr(args, attr))
        self.folder_name = args.folder_name
        self.multi = args.multi

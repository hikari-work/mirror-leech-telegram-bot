from . import LOGGER, bot_loop
from .core.telegram_manager import TgClient
from .core.config_manager import Config
from .core import fast_upload

Config.load()

# Patches Client.save_file, so it has to happen before any client is built.
fast_upload.install()


async def main():
    from asyncio import gather
    from .core.startup import (
        load_settings,
        load_configurations,
        save_settings,
        update_aria2_options,
        update_qb_options,
        update_variables,
    )

    await load_settings()

    await gather(TgClient.start_bot(), TgClient.start_user())
    await gather(load_configurations(), update_variables())

    from .core.torrent_manager import TorrentManager

    await TorrentManager.initiate()
    await gather(
        update_qb_options(),
        update_aria2_options(),
    )
    from .core.task_recovery import recover_tasks
    from .helper.util.telegraph_helper import telegraph
    from .modules import (
        initiate_search_tools,
        get_packages_version,
        restart_notification,
    )

    await gather(
        save_settings(),
        initiate_search_tools(),
        get_packages_version(),
        telegraph.create_account(),
    )
    # Sequentially, and not in the gather above. ``clean_all`` used to run here
    # on every boot, wiping the download directory and both engines whatever had
    # happened -- which is what made a restart cost every running task. It is
    # replaced by a pass that adopts what the engines are still holding and
    # sweeps only what nothing is coming back for.
    await recover_tasks()
    # After recovery, never beside it: the notifier reads the same task tables
    # and tells users to send again whatever has no row left. Racing the two
    # means it can read the rows before recovery has cleared the ones it placed.
    await restart_notification()


bot_loop.run_until_complete(main())

from .helper.util.bot_utils import create_help_buttons
from .helper.listeners.aria2_listener import add_aria2_callbacks
from .core.handlers import add_handlers, set_commands

add_aria2_callbacks()
create_help_buttons()
add_handlers()

bot_loop.run_until_complete(set_commands())
LOGGER.info("Bot Started! Commands registered.")
bot_loop.run_forever()

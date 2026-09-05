from . import LOGGER, bot_loop
from .core.telegram_manager import TgClient
from .core.config_manager import Config

Config.load()


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
    from .helper.util.files_utils import clean_all
    from .helper.util.telegraph_helper import telegraph
    from .modules import (
        initiate_search_tools,
        get_packages_version,
        restart_notification,
    )

    await gather(
        save_settings(),
        clean_all(),
        initiate_search_tools(),
        get_packages_version(),
        restart_notification(),
        telegraph.create_account(),
    )


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

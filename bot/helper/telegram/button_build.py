from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_STYLES = {
    "primary": ButtonStyle.PRIMARY,
    "blue": ButtonStyle.PRIMARY,
    "danger": ButtonStyle.DANGER,
    "red": ButtonStyle.DANGER,
    "success": ButtonStyle.SUCCESS,
    "green": ButtonStyle.SUCCESS,
}


def _button_style(style: str | ButtonStyle) -> ButtonStyle:
    """Normalize a button style, accepting the names the callers already use.

    Telegram names the three colours, while the menus in this repo grew up
    calling them ``blue``/``red``/``green``. Both spellings reach the same
    enum; an unknown name falls back to the default rather than failing a menu.
    """
    if isinstance(style, ButtonStyle):
        return style
    return _STYLES.get(style.lower(), ButtonStyle.DEFAULT)


class ButtonMaker:
    def __init__(self) -> None:
        self._button = []
        self._header_button = []
        self._footer_button = []

    def url_button(
        self,
        key: str,
        link: str,
        position: str | None = None,
        style: str | ButtonStyle = ButtonStyle.DEFAULT,
    ) -> None:
        button = InlineKeyboardButton(
            text=key, url=link, style=_button_style(style)
        )
        if not position:
            self._button.append(button)
        elif position == "header":
            self._header_button.append(button)
        elif position == "footer":
            self._footer_button.append(button)

    def data_button(
        self,
        key: str | int,
        data: str,
        position: str | None = None,
        style: str | ButtonStyle = ButtonStyle.DEFAULT,
    ) -> None:
        # ``key`` is sometimes a number -- a bitrate or a quality index -- and
        # Telegram wants every button label as a string.
        button = InlineKeyboardButton(
            text=str(key), callback_data=data, style=_button_style(style)
        )
        if not position:
            self._button.append(button)
        elif position == "header":
            self._header_button.append(button)
        elif position == "footer":
            self._footer_button.append(button)

    def build_menu(self, b_cols=1, h_cols=8, f_cols=8) -> InlineKeyboardMarkup:
        menu = [
            self._button[i : i + b_cols] for i in range(0, len(self._button), b_cols)
        ]
        if self._header_button:
            h_cnt = len(self._header_button)
            if h_cnt > h_cols:
                header_buttons = [
                    self._header_button[i : i + h_cols]
                    for i in range(0, len(self._header_button), h_cols)
                ]
                menu = header_buttons + menu
            else:
                menu.insert(0, self._header_button)
        if self._footer_button:
            if len(self._footer_button) > f_cols:
                [
                    menu.append(self._footer_button[i : i + f_cols])
                    for i in range(0, len(self._footer_button), f_cols)
                ]
            else:
                menu.append(self._footer_button)
        return InlineKeyboardMarkup(menu)

    def reset(self) -> None:
        self._button = []
        self._header_button = []
        self._footer_button = []

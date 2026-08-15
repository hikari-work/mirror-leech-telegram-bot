"""Host handler modules.

Importing this package imports every host module, which is what populates the
registry via the ``@register`` decorators.

Import order is **not** significant: each handler declares its position in the
old if/elif chain with ``order=`` and ``registry.resolve`` sorts on that. This
matters because matching is by substring, so one hostname can satisfy several
handlers at once (``racaty.mediafire.com`` contains both "racaty" and
"mediafire.com") and the chain answered with whichever branch came first --
branches that now live in different modules and interleave across them.
"""

from . import (
    bunkr,  # noqa: F401
    cloud,  # noqa: F401
    filehosts,  # noqa: F401
    gofile,  # noqa: F401
    imgbb,  # noqa: F401
    linkbox,  # noqa: F401
    lockers,  # noqa: F401
    mediafire,  # noqa: F401
    mega,  # noqa: F401
    sendcm,  # noqa: F401
    sharelinks,  # noqa: F401
    streaming,  # noqa: F401
    swisstransfer,  # noqa: F401
    terabox,  # noqa: F401
    vidara,  # noqa: F401
    vidoy,  # noqa: F401
)

"""Provider-agnostic machinery shared by the debrid resolvers.

The provider modules themselves stay where they were —
``alldebrid_resolver.py`` and ``torbox_resolver.py`` — so every import and
every test monkeypatch target keeps working. Only the parts both of them
were spelling out twice moved down here, into :mod:`base`.
"""

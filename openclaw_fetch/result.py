"""Result container: a dict that also supports attribute access.

    r = fetch("https://example.com")
    r.ok            # same as r["ok"]
    r["ok"]         # same as r.ok
    dict(r)         # plain copy
Json-serializable via ``json.dumps(r)``.

Caveat: ``r.items`` is ``dict.items`` (the method wins over attribute access),
so feed entries are always read as ``r["items"]`` or ``r.feed_items``.
"""


class Result(dict):
    __slots__ = ()

    @property
    def feed_items(self):
        """RSS/Atom entries; alias for ``r["items"]`` that cannot collide."""
        return self.get("items") or []

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        self[name] = value

    def __delattr__(self, name):
        try:
            del self[name]
        except KeyError:
            raise AttributeError(name) from None

    def to_dict(self):
        return dict(self)
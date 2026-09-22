"""Result container: a dict that also supports attribute access.

    r = fetch("https://example.com")
    r.ok            # same as r["ok"]
    r["ok"]         # same as r.ok
    dict(r)         # plain copy
Json-serializable via ``json.dumps(r)``.
"""


class Result(dict):
    __slots__ = ()

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
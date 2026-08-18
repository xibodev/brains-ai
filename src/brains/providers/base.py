from collections.abc import Iterator
from typing import Any, ClassVar


class Provider:
    #: Marks a provider as a development stub (e.g. ``echo``) rather than a
    #: real upstream. Downstream consumers (the savings ledger, the admin
    #: UI's Providers rail) use this to keep synthetic traffic from being
    #: counted as real usage or sold as a real integration. Real providers
    #: leave this at the default ``False``.
    is_stub: ClassVar[bool] = False

    def complete(self, model: str, messages: list[dict], **kwargs: Any) -> dict:
        raise NotImplementedError

    def stream(self, model: str, messages: list[dict], **kwargs: Any) -> Iterator[str]:
        raise NotImplementedError

    def list_models(self) -> list[dict[str, Any]]:
        """Best-effort discovery of models this provider exposes.

        Returns a list of ``{"id": str, "vendor": str | None, "label": str | None}``
        dicts. The default implementation returns ``[]`` so providers without a
        catalog endpoint (or temporarily unreachable upstreams) simply don't
        contribute autocomplete suggestions — callers MUST tolerate an empty list
        and never crash on it.
        """
        return []

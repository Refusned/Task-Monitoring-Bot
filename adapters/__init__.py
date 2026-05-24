"""Exchange adapters: thin base + Panel vs TaskExchange variants."""

from adapters.advego import AdvegoAdapter
from adapters.base import Capability, ExchangeAdapter, PanelAdapter, TaskExchangeAdapter
from adapters.fake import FakePanelAdapter, FakeTaskExchangeAdapter
from adapters.ipgold import IpgoldAdapter
from adapters.prskill import PrskillAdapter
from adapters.smmcode import SmmcodeAdapter
from adapters.unu import UnuAdapter

__all__ = [
    "AdvegoAdapter",
    "Capability",
    "ExchangeAdapter",
    "FakePanelAdapter",
    "FakeTaskExchangeAdapter",
    "IpgoldAdapter",
    "PanelAdapter",
    "PrskillAdapter",
    "SmmcodeAdapter",
    "TaskExchangeAdapter",
    "UnuAdapter",
]

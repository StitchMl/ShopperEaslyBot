from __future__ import annotations

from .offer_link_checks import OfferLinkCheckStoreMixin
from .offer_merges import OfferMergeStoreMixin
from .offer_records import OfferRecordStoreMixin
from .offer_sources import OfferSourceStoreMixin


class OfferStoreMixin(
    OfferRecordStoreMixin,
    OfferLinkCheckStoreMixin,
    OfferMergeStoreMixin,
    OfferSourceStoreMixin,
):
    pass
